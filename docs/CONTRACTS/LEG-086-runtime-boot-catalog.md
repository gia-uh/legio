# LEG-086 — Runtime boot catalog: topological order + pools resolution (§8/§4.3)

- **Status:** DRAFT (awaiting maintainer approval)
- **Rasante:** R-8
- **GitHub issue:** #50 (provisional — no `gh` access; the maintainer holds it)
- **Source:** `docs/PLAN.md` (R-8), `docs/AGENT_LIFECYCLE.md` §4.3/§4.7/§8,
  `docs/AGENT_LIFECYCLE.md` §2 (beaver scopes), LEG-080 (`PoolsConfig`,
  `src/legio/config.py`)
- **Depends on:** LEG-085 (`legio.runtime.Runtime.create_class`), LEG-084
  (`legio.registry.Registry`), LEG-080 (`PoolsConfig.resolve`),
  `legio.patterns.schema1` `Catalog`/`AgentSpec`

## Goal

Implement the **initial-state bootstrap** of §8 as a Runtime-surface operation:
`Runtime.create_from_catalog(catalog, *, pools, spec_yamls, pool_override)`
brings every **served** class up **in topological order** (leaves first) so that
dependents find their dependencies already created (and enabled) and are born
enabled naturally (§4.2/§8 step 4). It wires **LEG-080 pools** into class
creation: each class's pool is resolved as explicit invocation override >
`per_pattern` > `per_kind` > `default` > 1 (§4.3), passing the resolved intent
into `create_class(spec, *, spec_yaml, pool)`. Step 4 of the approved 4-step R-8
plan (Manager → Registry → Runtime → **boot re-audit + pools wiring**). Additive:
it adds one method (+ two small private helpers) to the `legio.runtime.Runtime`
of LEG-085; no existing surface changes.

## Scope

- **In scope:** the topological-ordering helper (leaves-first, §8 step 2) over
  the served specs of a `Catalog`; the cycle detection (§8 step 6 — rejected
  **before** anything is recorded); the pool resolution helper (§4.3/LEG-080);
  the `create_from_catalog` orchestration (per class: `create_class` with the
  resolved pool and the spec's YAML for the runtime cache, §8 step 3); the
  `spec_yamls` map (name → YAML text; absent entries cache nothing yet stay
  created); born-disabled for classes whose dependencies are not satisfied
  (§8 step 5). `create_class` gains an optional `spec_yaml` (`str | None`,
  default `None` → no cache) so the catalog path can cache when the YAML is
  known and skip it otherwise.
- **Out of scope:** the **agent-loop interior** — the real bring-up callable
  that starts the materialized agents' `run()` loops and the "n agents on the
  same queue process n items concurrently" acceptance of LEG-080. That is the
  boot wiring between `materializer.NodeRuntime` and the Runtime/Manager/
  Registry triangle, and the interior of instance execution — left to a
  maintainer decision (open item). This slice proves the *create-side* of pools:
  the resolved count of instances actually brought up and recorded (= the real
  count, §8 step 3 mirror rule). The CLI `--pool N` verb (LEG-081) is out of
  scope; `pool_override` is the programmatic seam that verb would feed.

## Contract & design

### `create_from_catalog(catalog: Catalog, *, pools: PoolsConfig, spec_yamls: Mapping[str, str] | None = None, pool_override: int | None = None) -> None`

1. **Order** — topologically sort the *served* names of `catalog` (Kahn's
   algorithm over the composite `branches` that reference remaining served
   classes). A spec whose dependency is not in the served set is treated as
   having no unresolved dependency (it is created; born state follows what
   actually exists in the Registry, §8 step 5).
2. **Cycle** — if the queue sticks with served classes left, raise
   `RecoverableError` naming the stuck classes **before** any class of the
   catalog has been created (rule 9: nothing recorded for a broken graph).
3. **Pool** — per class: `pool = pool_override if pool_override is not None
   else (pools.resolve(name, per_kind=spec.kind) or 1)`. `0` borns the class
   disabled (§4.3). Pass `pool` into `create_class`.
4. **Cache** — per class, pass `spec_yamls.get(name)` as `spec_yaml`; a present
   value is cached by the Registry (§4.7/§8 step 3); an absent one caches
   nothing (the class still exists). Unknown keys in `spec_yamls` are ignored
   (only served specs create).
5. **Born state** — `create_class` already decides it: enabled iff `pool > 0`
   AND `dependencies_satisfied(name)` (a dependency created earlier in the
   order is enabled, so dependents are born enabled — §4.2/§8 step 4). Anything
   unsatisfied stays disabled but visible (§8 step 5).
6. **Intent vs real** — the catalog records the *real* count of instances
   actually brought up and confirmed `running` (§8 step 3, mirror rule); the
   resolved pool is only the intent fed to `create_class`.

### `create_class` signature evolution (optional `spec_yaml`)

LEG-085 declared `create_class(spec, *, spec_yaml: str, pool: int = 1)`. This
slice makes `spec_yaml` optional (`str | None = None`): `cache_spec` runs only
when the YAML text is provided. All LEG-085 callers (which always pass
`spec_yaml`) keep working unchanged; the boot catalog path may cache the YAML
only when it has it.

### Logging (rule 11)

- INFO `runtime create_from_catalog classes=<n> order=<names>` at completion.
- WARNING `runtime catalog cycle classes=<names>` before the reject.
- `create_class`'s existing no-op INFO/WARNING and the per-bring-up logs cover
  the per-class/instance detail.

## Error policy

- Dependency cycle → `RecoverableError` (visible, naming the stuck classes),
  raised before anything is recorded.
- No new error types; all other failures (bring-up confirmation, unknown
  classes, etc.) surface exactly as in LEG-085's error policy (rule 9).

## Acceptance criteria

- Given a served catalog whose dependency graph is a DAG, all classes exist in
  the Registry afterwards and every class whose dependencies are satisfied is
  born **enabled** — including dependents (leaves first).
- The resolved pool per class: explicit override > `per_pattern` > `per_kind` >
  `default` > 1; the number of recorded instances equals the resolved intent;
  a `0` resolution borns the class disabled with no instances.
- A `per_kind` resolution applies to the class kinds (tool/linguistic, Schema 1
  KINDS); composite classes resolve via `per_pattern`/`default` (they carry no
  kind).
- A dependency cycle raises `RecoverableError` and leaves `list_classes()`
  empty (nothing recorded).
- A class with an unsatisfied dependency is recorded disabled (its instances,
  if any, disabled too — §4.3) — never silently dropped.
- The runtime YAML cache holds the YAML for every class whose name is in
  `spec_yamls`; absent YAMLs leave no cache entry and no failure.
- No regressions: the full suite plus the new tests are green (ruff + pytest +
  pyright).

## Verification

- `uv run pytest tests/test_leg086_boot_catalog.py -q` — 11 passed (red first:
  missing method).
- `uv run pytest -q` — 278 passed (267 + 11; no regressions).
- `uv run ruff check src/legio/runtime tests/test_leg086_boot_catalog.py` —
  clean.
- `uv run pyright src/legio/runtime tests/test_leg086_boot_catalog.py` — 0.

## Open items (to raise with the maintainer)

- The **agent-loop interior** is the remaining half of §8 step 3/step 7: the
  real bring-up callable (registered under `BRING_UP_TASK`) must start the
  materialized agents' `run()` loops so a pool of n instances drives the same
  class queue concurrently (the LEG-080 accept "n agents process n items"). The
  default parked-gate vehicle of LEG-085 keeps instances parked; wiring
  `materializer.NodeRuntime` (agents) into the Runtime/Manager/Registry will
  replace it. History: the R-8 plan calls this the remaining "boot re-audit".
- Whether `boot_node` itself should drive `create_from_catalog` from its loaded
  `Catalog`+`PoolsConfig`, or the decision stays higher (the caller/CLI), needs
  a maintainer call.
- GitHub issue numbers for LEG-083/084/085/086 are provisional.