# LEG-103 — Audit hardening batch (subagent design/coupling audit, sessions 87-88)

- **Status:** DRAFT overall; Slice 1 (tool execution semantics) APPROVED by maintainer direction on 2026-09-16 (session 89); Slice 2 (pending-gather wakeup) APPROVED by maintainer direction on 2026-09-16 (session 93, Option A; beaver events reserved for a future version); Slice 3 (fan-in exclusion) APPROVED by maintainer direction on 2026-09-16 (session 95, per-slot keys); Slice 4 (legacy fed plane) APPROVED by maintainer direction on 2026-09-16 (session 97, delete with type relocated); Slice 5 (minors/notes batches 5a–5e) APPROVED by maintainer direction on 2026-09-16 (session 99, batch plan); Slice 6 (complete execution semantics: always-off-loop + general awaitable + shape rejection) APPROVED on 2026-09-16 (session 102 scope/semantics, always-to-thread over contract amendment); Slice 7 (docs/trivial batch m3–m6/n1/n7/n8) APPROVED on 2026-09-16 (session 102);
Slice 8 (post-re-audit hardening: m7/m8, coalesce ordering, fan-out race,
finite budgets) APPROVED on 2026-09-16 (session 104: maintainer direction
"check everything and implement what's necessary");
Slice 9 (fresh-audit hardening: strict numerics, validate-first destroy,
loser cleanup, cancel-safe kick, prose/dependency/test gaps) APPROVED on
2026-09-16 (session 108).
- **Rasante:** R-10 (hardening)
- **GitHub issue:** #51 (created + closed with verification, session 106)
- **Source:** `docs/PLAN.md` (LEG-103); audit evidence in `docs/JOURNALS/2026-09-15.md` (86i), `docs/JOURNALS/2026-09-16.md` (87-88)
- **Depends on:** LEG-013 (Schema 3), LEG-022 (ToolAgent), LEG-040/042 (composite), LEG-015 (federation), LEG-081 (CLI)

## Goal

Fix, one by one and contract-first, the design/coupling findings of the
sessions 87-88 full audit, without changing the domain-free, polling-only,
transport/lifecycle-separated architecture.

## Backlog (audit order)

1. **Slice 1 — tool execution/policy semantics (Major 1).** APPROVED 2026-09-16.
2. **Slice 2 — composite pending-gather wakeup (Major 2).** APPROVED 2026-09-16 (Option A).
3. **Slice 3 — composite fan-in exclusion (Major 3).** APPROVED 2026-09-16 (per-slot keys).
4. **Slice 4 — legacy global `fed` plane (Major 4).** APPROVED 2026-09-16 (delete).
5. **Slice 5 — CLI federation token ordering + verified minors/notes.** APPROVED 2026-09-16 (batches 5a–5e).
6. **Slice 6 — complete tool execution semantics (Majors M1+M2).** APPROVED 2026-09-16.
7. **Slice 7 — docs/trivial batch (minors m3–m6, notes n1/n7/n8).** APPROVED 2026-09-16.
8. **Slice 8 — post-re-audit hardening (minors m7–m9 decided/fixed, notes
   n2–n6/n9–n10 decided/fixed).** APPROVED 2026-09-16.
9. **Slice 9 — fresh-audit hardening (11 minors F1–F11).** APPROVED 2026-09-16.

## Slice 1 contract (APPROVED)

Amends the undecided execution mechanics left open by LEG-013 (“async
execution mechanism … decided at implementation”):

- Tools may be **sync or async callables**; async tools are awaited, never
  wrapped raw. Async generators are rejected loudly (not a valid tool shape).
- Sync tools run **off the event loop** (`asyncio.to_thread`) so one slow call
  cannot stall every pump.
- The declared per-call **`timeout` is enforced** for both shapes via
  `asyncio.wait_for`; expiry surfaces as a visible `TimeoutError` result
  (rule 9), never silent.
- **`retries` stays 0**: any nonzero declared value fails loudly without
  retrying (the engine never retries a step).

## Acceptance criteria (Slice 1)

- An async tool executes and its value (not a coroutine) lands under `output_as`.
- A slow sync tool and a slow async tool each exceed a small policy timeout and
  surface `TimeoutError` visibly.
- A nonzero `retries` declaration fails loudly without any retry.
- An async-generator tool fails loudly.
- Full suite + ruff + pyright green; no other behavior changed.

## Slice 2 contract (APPROVED)

Replaces the unbounded `asyncio.sleep(0.01)` pending-gather re-poll with
Option A (dual concurrent gets rejected: cancelling a beaver `get` can strand
an already-popped item between DELETE and return):

- With a pending fan-out and both inlets dry, the tick suspends on the class
  inbox with a **bounded substrate wait** (`get(block=True,
  timeout=gather_budget)`, default `0.5` s), then re-polls gathering once.
- Inbox work/control wakes the tick **immediately** — control between
  dispatches is preserved and the 86e deadlock fix holds with no legio timer.
- Join liveness is unchanged: no spurious missing-branch failures; the fast
  paths (non-blocking inbox/gather polls first) are untouched.
- `gather_budget` is an explicit constructor seam (must be positive, refused
  loudly otherwise) so tests drive small budgets; the materializer keeps the
  default.

## Acceptance criteria (Slice 2)

- Pending + dry inlets: the tick suspends ~budget on the inbox (no ~0.01 s
  spin-back).
- No sub-0.05 s `asyncio.sleep` fires during an idle pending tick (only the
  substrate's own cadence may appear).
- A pre-deposited gather result is still collected, completing the join.
- New inbox work while pending is fanned out at once, never waiting the budget.
- Non-positive budgets are refused loudly at construction.
- Full suite + ruff + pyright green; no other behavior changed.

## Slice 3 contract (APPROVED)

Replaces the shared-mutable join record with per-slot keys (single-owner and
beaver-lock designs rejected: affinity machinery and lock TTL semantics are
foreign concepts to the flow; the atomic close below needs neither):

- Fan-out writes one slot key per branch (`<task_id>:<branch_id>` →
  `{index, result}`) in a dedicated slots scope, then the continuation record
  carrying only the ordered `expected` branch list (record presence means all
  slot keys exist).
- Each `_fan_in` worker writes **only its own slot key**, then reads the
  expected slots; partial joins return without writing anything shared.
- The close is arbitrated by **one atomic op**: deleting the continuation
  record — the first deleter builds and resumes, a loser finds it gone
  (`KeyError`) and stands down with a debug event.
- Unknown branch / missing slot / missing fan-out stay loud `ValueError`s
  (rule 9 preserved); slot keys are deleted after resume (best-effort
  cleanup); crash semantics unchanged (no replay anywhere in the engine).

## Acceptance criteria (Slice 3)

- Fan-out layout: continuation with ordered `expected`, no embedded slots;
  slot keys carry fan-out index and empty result.
- Two concurrent branch returns join exactly once: one advance, no stranded
  record, no leftover slots.
- Unknown-branch and no-fan-out results stay loud errors.
- Full suite + ruff + pyright green; no other behavior changed.

## Slice 4 contract (APPROVED)

Delete (isolate/bless rejected: hiding or blessing the module-global `_NODES`
keeps the no-global-state violation and the wrong-plane import hazard):

- Delete `src/legio/fed.py` (the LEG-015-era in-memory symmetric plane:
  module-global node registry, process-resident queues and outbox).
- Relocate the one production-used value, `AgentInterface`, into
  `legio.federation` (frozen dataclass, same shape); production imports the
  type from there.
- Retire `tests/test_leg015_federation.py`: every behavior it pins is covered
  on the production plane — catalog by `test_leg090`, interface mismatch by
  `test_leg091`/`test_leg092`, deposit by `test_leg092`, dedupe and outbox
  poll/ack by `test_leg093`/`test_leg095_result_drain`.
- Historical journal entries keep mentioning `legio.fed`; they are immutable
  history and stay as-is.

## Acceptance criteria (Slice 4)

- No `legio.fed` module, import, or global state remains in `src` or tests.
- Full suite + ruff + pyright green with no legacy-plane tests.

## Slice 5 contract (APPROVED)

Five batches, one commit each; decisions made inline where the audit left
open questions:

- **5a (CLI):** refuse `--federation` without token **before** boot (no
  substrate side effects); YAML collection splits via the parser
  (`loader.split_yaml_documents`), single ingestion path.
- **5b (layering/hygiene):** `ActivityState` owned by `naming` (registry
  re-exports); peer map through the explicit `Runtime` constructor seam;
  duplicate ledger init removed; `remove_class` snapshots keys before
  deleting; `castor` reference dropped.
- **5c (observability):** INFO on token register/revoke (never the secret);
  pause/resume/cancel logs name the TTL and expiry-reverts-to-run is
  documented; error-code fallback is deterministic md5 (API's hardcoded
  taxonomy stays); ARCH wording reflects inline enforcement owned by the
  `AuthMiddleware` policy (wiring the surface through it rejected as churn);
  result-drain kicks coalesce via a best-effort in-memory set (full task GC
  stays an open R-10 question).
- **5d (clocks/resources/robustness):** drain wait on a monotonic deadline;
  proxy deposit client has an owned lifecycle (`ensure_client`/`aclose`,
  injected clients untouched, closed at CLI teardown); parked-hold crash
  loss accepted and documented (no requeue churn).
- **5e (docs/notes):** prose fixed to English (AGENTS, CONTRIBUTING, PLAN
  headings, payload, journal template); the `Rasante:` metadata field label
  across contracts is retained as established vocabulary (renaming ~40 files
  is churn without design value); loader TODO resolved to execution-time
  only; no-catalog API fallback documented as embedded/test-only (booted
  nodes always pass a catalog); proxy lifecycle hazard documented; implicit
  env/host/clock seams unchanged (explicit seams tested).

## Acceptance criteria (Slice 5)

- Refusal without token creates no database file.
- Literal-block `---` never splits YAML collection.
- Agents import vocabulary from `naming`; peer filter works via constructor.
- Token/pause events logged; error codes deterministic; drain kicks coalesce.
- Drain clock monotonic; proxy client closed at teardown; parked semantics
  documented.
- No non-English prose in living docs/code (metadata field label excepted).
- Full suite + ruff + pyright green; no other behavior changed.

## Tests

- `tests/test_leg022_toolagent.py`: five new contract tests (red first).
- `tests/test_tools.py`: async/slow/asyncgen fake tools (domain-free fixtures).
- `tests/test_leg103_slice2_wakeup.py`: five new contract tests (red first).
- `tests/test_leg103_slice3_fanin.py`: four new contract tests (red first).
- `tests/test_leg103_slice5_batches.py`: Slice 5 batch tests (red first);
  the token-order case extends `test_leg081_cli.py` in place.
- `tests/test_leg022_toolagent.py` (Slice 6): six new contract tests (red
  first); `tests/test_tools.py` gains the Slice 6 domain-free shapes (async
  callable instance, sync-returns-coroutine, sync generator, asyncgen-returning
  shape, thread probe).
- Slice 7: no new behavior tests (docs/`__all__`/dead-code/log-wording batch);
  the `legio.fed` logger-string case is updated in place in
  `tests/test_legio_logging.py`.

## Slice 6 contract (APPROVED)

Completes the Slice 1 execution model (Session 101 majors M1+M2). The code is
fixed to match the declared contract — the contract is not amended to allow
blocking:

- Sync tools ALWAYS run off the event loop (`asyncio.to_thread`), whether or
  not a timeout is declared (polling-only rule 8; the "Sync tools run off the
  loop" sentence stays true as written). The timeout, when declared, still
  bounds the wait via `asyncio.wait_for`; the stray-thread-on-timeout
  semantics from Slice 1 carry over unchanged.
- General awaitable rule: whatever the call returns, if it is awaitable it is
  awaited (covers async callable instances with an async `__call__`, sync
  callables returning a coroutine/future, and chained awaitables — awaited in
  a loop until a plain value remains), still under the declared timeout.
- Loud rejection of non-value shapes (rule 9): async-generator functions and
  async-generator objects, sync-generator functions and sync-generator
  objects all fail with a visible `TypeError` result instead of leaking an
  unconsumed iterator into the payload.

## Acceptance criteria (Slice 6)

- A sync tool without a timeout executes off the loop (its thread differs
  from the event-loop thread) and its value lands under `output_as`.
- An async callable instance is awaited: its value (not a coroutine) lands
  under `output_as`.
- A sync callable returning a coroutine is awaited: its value lands under
  `output_as`.
- A sync generator tool fails loudly (visible `error`, never a leaked
  generator).
- A sync shape returning an async generator fails loudly.
- Full suite + ruff + pyright green; no other behavior changed.

## Slice 7 contract (APPROVED)

Docs/trivial batch with zero behavior change:

- **m3:** `docs/ARCHITECTURE.md:163` drops the "guarded by a per-tool
  concurrency semaphore" promise (no such mechanism exists after the LEG-082
  withdrawal; `semaphore` stays a future scope per §2) — the shared resource
  is described as shared with no engine-side cap.
- **m4:** `docs/ARCHITECTURE.md:217` drops the "under lock" fan-in sentence —
  bookkeeping is per-slot keys plus the atomic continuation-delete close
  (Slice 3).
- **m5:** `docs/CONTRACTS/LEG-040-composite-agent.md:68` drops the same stale
  "under a lock" sentence for the same per-slot mechanism.
- **m6:** `AgentInterface` joins `__all__` in `src/legio/federation.py`.
- **n1:** the `"legio.fed"` logger string in `tests/test_legio_logging.py`
  becomes the production `"legio.federation"` plane name.
- **n7:** the dead `_CREATE_VERBS` set in `src/legio/cli.py` is deleted (only
  `_LIFECYCLE_VERBS` dispatches).
- **n8 (budget log nit):** the `gather_budget` validation message no longer
  says "zero" for every non-positive value, and construction logs the budget
  at DEBUG (rule 11).

## Acceptance criteria (Slice 7)

- No "per-tool concurrency semaphore" promise remains in ARCH §5; no "under
  lock" fan-in sentence remains in ARCH §7 or LEG-040 §Fan-in.
- `AgentInterface` is importable from `legio.federation.__all__`.
- No `legio.fed` string remains in tests; no `_CREATE_VERBS` symbol remains
  in `src`.
- Non-positive `gather_budget` still refused loudly; construction emits the
  budget at DEBUG.
- Full suite + ruff + pyright green; no behavior changed.

## Validation case

- Existing `transform` fake-tool paths unchanged and green.

## Slice 9 contract (APPROVED)

Fresh-audit hardening from the Session 106 re-audit (0 blocking, 0 major).
Every finding below was independently verified against source before fixing:

- **F1/F2 (bool coercion hole):** pydantic coerces `True → 1` before
  `mode="after"` validators run, so `timeout: true` / `drain_timeout: true` /
  `gather_budget: true` were silently accepted as `1`/`1.0`. All three seams
  (`ToolPolicy`, `LifecycleParams`, `CompositeAgent`) reject booleans
  explicitly. `retries: true` now fails at load too (a distinct authoring
  error from Slice 1's nonzero-int case, which still passes load and fails
  loudly at execution).
- **F3 (PoolsConfig type guard):** pool sizes must be genuine integers
  (`type(x) is int`, `>= 0`) — no silent `"3" → 3` / `1.0 → 1` / `True → 1`
  coercion. `port`/`max_tokens_per_batch` left alone (fail downstream-loud;
  out of scope).
- **F4 (`control_ttl` unvalidated):** `Manager` requires a finite number
  `> 0` at construction (same budget discipline as Slice 8).
- **F5 (validate-before-mutate):** `destroy_class` rejects an unknown `mode`
  before touching the gate — a failed call leaves no side effect.
- **F6 (loser slot leak):** the atomic-close loser deletes the slot key it
  just wrote (best-effort, `KeyError`-tolerant) before standing down. Loser
  keys are disjoint from the winner's `expected` set by construction
  (uuid4 branch ids), so the cleanup cannot disturb the winner.
- **F7 (cancel-safe kick):** the coalesce-flag discard runs on
  `BaseException` (covers `CancelledError`) and always re-raises.
- **F8 (stale semaphore prose):** CONTRIBUTING's "per-resource semaphores"
  rule reworded to the no-engine-side-cap truth (twin of Slice 7's ARCH fix).
- **F9 (listed-but-unused dependency):** `pydantic-settings` removed from
  `pyproject.toml`, `docs/DEPENDENCIES.md`, and the ARCH table (zero imports
  in `src`/`tests`; env config reads `os.environ` directly). Lockfile
  regenerated.
- **F10 (vacuous test):** `test_reuse_by_position` implemented for real — one
  shared agent definition reused by two composites validates in both.
- **F11 (yaml-cache error shape):** the per-file wrap extends to `OSError`
  (unreadable file, e.g. a directory named `*.yaml`) and
  `UnicodeDecodeError` (non-UTF8 bytes are a `ValueError`, not a `YAMLError`)
  alongside `YAMLError`.
- **N9 (docstring example):** `resolve_parameters` illustrates
  `{input_as}.{key}` instead of the unresolvable `{payload.text}`.

## Acceptance criteria (Slice 9)

- Bool/float/string pool sizes and bool budgets/timeouts fail loudly at the
  boundary where they are declared; genuine ints and positive finite numbers
  still pass.
- `Manager(control_ttl=NaN|inf|negative|bool)` raises at construction.
- `destroy_class(name, mode="bogus")` raises with the gate still open.
- A simulated lost close deletes only the loser's slot and keeps the record.
- A cancelled kick submit clears the coalesce flag and propagates.
- No `pydantic-settings` in dependencies/docs; `uv lock` + full suite green.
- The reuse test loads two composites sharing one agent definition.
- Unreadable/non-UTF8 pattern files fail cache collection naming the file.
- Full suite + ruff + pyright green; no other behavior changed.

## Slice 8 contract (APPROVED)

Post-re-audit hardening from the maintainer's "check everything" direction
(session 104). Every remaining Session 101 minor/note was re-verified against
source; the fixes below are the necessary ones, the rest are explicit
no-change decisions recorded here:

- **m7 (ToolPolicy.timeout unvalidated):** `ToolPolicy` refuses a non-positive
  or non-finite `timeout` at load (`ConfigError` via `load_tools_file`;
  `retries` keeps its Slice 1 semantics — `None`/`>= 0` passes load so a
  nonzero value still fails loudly at execution without retrying). The
  direct-registry path (`AvailableToolsRegistry.declare`, bypasses file
  validation) is guarded at execution: `_tool_policy` raises a loud
  `ValueError` naming the policy instead of handing a meaningless timeout to
  `asyncio.wait_for`.
- **Finite budgets (NaN/inf note):** `NaN` slips through every `<= 0` check
  (`NaN <= 0` is `False`) and `inf` passes as a pseudo-unbounded wait. All
  three budget seams now require a finite value: `CompositeAgent`
  `gather_budget`, `LifecycleParams` drain budgets, and the `ToolPolicy`
  timeout above.
- **m8 (bare `safe_load` in `_collect_spec_yamls`):** a `YAMLError` is wrapped
  in a `ConfigError` naming the file (same convention as `config._read_yaml`)
  instead of escaping raw.
- **Coalesce-add ordering (note):** `_kick_result_drain` added the agent to
  `_drain_inflight` *before* `submit_task`; a submit failure left the flag set
  forever and stranded the queue. The flag is now discarded when the submit
  raises (the error still propagates loudly, rule 9).
- **Duplicate fan-out race (note):** two concurrent fan-outs for one task
  could both pass the exists-check, write orphan slots under different
  branch ids, and let the last record win (the loser's branches then fail as
  unknown). After writing its slots each worker re-checks the continuation:
  on a lost race it deletes only its own slots and stands down, leaving the
  winner's fan-out intact. A residual same-instant window keeps today's
  behavior (loud, never silent).
- **Registry import-source tidy (n10):** `Runtime` imports the vocabulary it
  uses (`ActivityState`) from its owner `legio.naming`, not via the
  `legio.registry` re-export (Slice 5b ownership).
- **ARCH Slice 2 completeness (n9):** ARCH §3/§7 now name the bounded
  substrate wait (`get(block=True, timeout=gather_budget)`, default 0.5 s) so
  the architecture text matches the shipped Slice 2 mechanism.

Decided with no code change (verified, recorded):

- **m9 (post-close duplicates vs the standing loop):** kept loud. Downgrading
  to warning would contradict the Slice 3 acceptance criteria (unknown-branch
  and no-fan-out stay loud errors) and the engine-wide poison-item posture
  (`_process_inbox_item` likewise crashes loudly instead of skipping). A
  duplicate after close is either a redelivery (benign, and its loud error
  names the task/branch for the operator) or a genuine routing anomaly —
  neither may die silently.
- **Strip edge (note):** probed `split_yaml_documents`/`_strip_separator_lines`
  against comment-only docs, CRLF, trailing-space separators, `...` end
  markers, BOM, empty docs, and `--`-in-literal-block — all split correctly;
  no change.
- **Upgrade migration (note):** exact-match `schema_version` rejection stays.
  There is no live-migration story by design: upgrades drain and redeploy
  (fail-fast versioning, LEG-011); a lenient major-only gate would be a new
  semantic requiring its own spec.

## Acceptance criteria (Slice 8)

- Negative/zero/NaN tool timeouts fail at load (`ConfigError`); a bad timeout
  through the direct-registry path fails loudly at execution naming the
  policy; `retries: 1` still passes load and fails at execution (Slice 1).
- NaN/inf `gather_budget` and NaN/inf lifecycle budgets are refused loudly.
- A syntactically broken pattern file fails YAML-cache collection with a
  `ConfigError` naming the file.
- A failed drain-kick submit clears the coalesce flag (loud error, re-kick
  works).
- A lost fan-out race deletes only the loser's slots and keeps the winner's
  record; sequential double fan-out stays a no-op.
- `ActivityState` in `runtime` imports from `legio.naming`; ARCH names the
  bounded substrate wait.
- Full suite + ruff + pyright green; no other behavior changed.

## Definition of done (per slice)

- Spec slice approved, red tests, green implementation, full suite + lint +
  typecheck green, journal entry appended, maintainer closes the issue.
