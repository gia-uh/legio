# LEG-103 — Audit hardening batch (subagent design/coupling audit, sessions 87-88)

- **Status:** DRAFT overall; Slice 1 (tool execution semantics) APPROVED by maintainer direction on 2026-09-16 (session 89); Slice 2 (pending-gather wakeup) APPROVED by maintainer direction on 2026-09-16 (session 93, Option A; beaver events reserved for a future version); Slice 3 (fan-in exclusion) APPROVED by maintainer direction on 2026-09-16 (session 95, per-slot keys); Slice 4 (legacy fed plane) APPROVED by maintainer direction on 2026-09-16 (session 97, delete with type relocated); Slice 5 (minors/notes batches 5a–5e) APPROVED by maintainer direction on 2026-09-16 (session 99, batch plan); Slice 6 (complete execution semantics: always-off-loop + general awaitable + shape rejection) APPROVED on 2026-09-16 (session 102 scope/semantics, always-to-thread over contract amendment); Slice 7 (docs/trivial batch m3–m6/n1/n7/n8) APPROVED on 2026-09-16 (session 102);
Slice 8 (post-re-audit hardening: m7/m8, coalesce ordering, fan-out race,
finite budgets) APPROVED on 2026-09-16 (session 104: maintainer direction
"check everything and implement what's necessary");
Slice 9 (fresh-audit hardening: strict numerics, validate-first destroy,
loser cleanup, cancel-safe kick, prose/dependency/test gaps) APPROVED on
2026-09-16 (session 108);
Slice 10 (design/coupling audit hardening: version-aware delegation,
guarded executor, strict verbs, taxonomy, intake hygiene) APPROVED on
2026-09-16 (session 110: maintainer direction "resuelve esto");
Slice 11 (fifth-audit hardening: middleware predicate, yaml/config parity,
tool/taxonomy gaps, bool residuals, leg order uniformity, clear logging)
APPROVED on 2026-09-16 (session 112: maintainer direction
"arregla todos los minors");
Slice 12 (sixth-audit hardening: uniform pattern policy, seed-envelope
reads, loader YAMLError wrap, token/config/CLI/log hardening) APPROVED on
2026-09-16 (session 120: maintainer direction "implementa los planes tanto
de los majors como los minors");
Slice 13 (seventh-audit hardening: registry index, composite counter,
proxy surface, log/ledger/seed hygiene) APPROVED on 2026-09-16 (session
122: maintainer direction "arregla todo").
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
10. **Slice 10 — design/coupling audit hardening (2 majors F1–F2, 9 minors
    F3–F11).** APPROVED 2026-09-16.
11. **Slice 11 — fifth-audit hardening (8 minors M1–M8).** APPROVED
    2026-09-16.
12. **Slice 12 — sixth-audit hardening (majors M1/M3/M4(c), minors
    m1–m10; M2 withdrawn, M4(a)/(b) withdrawn).** APPROVED 2026-09-16.
13. **Slice 13 — seventh-audit hardening (3 majors M1–M3, 11 minors
    m1–m11).** APPROVED 2026-09-16.

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

## Slice 10 contract (APPROVED)

Design/coupling audit hardening from the Session 109 re-audit (0 blocking).
Decisions first (scope control), then fixes:

- **F1 (major, version-blind delegation):** the Phase-3 cross-node leg gains
  the version gate the work-item leg already has. `DepositRequest` carries
  `schema_version` (defaulting to the node's own `SCHEMA_VERSION`, so old
  clients keep working); the owner refuses a mismatch with 409
  `interface_mismatch` (same code as work-items); the proxy stamps the
  current version on every remote `put`. A stale-version delegation therefore
  fails LOUDLY at deposit (visible `RecoverableError` → error result) instead
  of executing silently. `StepResolver` stays the pure, tested decision unit
  for work-item-style delegation; the static table routes by name and the
  owner gate-checks the version — the federation.py agreement comment is
  reworded to this truth.
- **F2 (major, unguarded executor):** `Manager.run()` logs
  `logger.exception` with the task id and re-raises on any escaping dispatch
  error (the agent crash posture of `AgentBase._process_inbox_item`).
  Cancellation (`CancelledError`, `BaseException`) still propagates
  unlogged — a cancelled pump is a clean host shutdown, not a crash.
- **F3 (bool timeout on the direct-registry seam):** `_tool_policy` rejects a
  boolean timeout loudly, naming the tool (parity with the Slice 9 pydantic
  seams).
- **F4 (silent count/pool no-ops):** `create_instance(count < 1)` and
  `create_class(pool < 0)` raise `ValueError` at verb entry.
- **F5 (taxonomy):** `ContractError` derives from `UnrecoverableError`
  (authoring/validation fatal), not bare `RuntimeError`. Behavior unchanged
  (still surfaced as error results).
- **F6 (untyped escapes):** tool load failure raises `UnrecoverableError`
  (fatal authoring: bad dotted path), not bare `RuntimeError`; both CLI
  boundaries map `KeyError`/`ValueError`/`OSError` to the loud
  `legio error:` exit instead of a traceback (genuine bugs still traceback).
- **F7 (node-op intake hygiene):** when the drain-task submit fails,
  `deposit_node_op` best-effort removes the just-queued intent (a concurrent
  deposit's drain covers leftovers either way) and the original error
  propagates loudly. Documented race, same class as the Slice 8 kick fix.
- **F8 (cache/loader duplicate divergence):** `_collect_spec_yamls` raises
  `ConfigError` naming both files on a duplicate pattern name (the loader
  already rejects duplicates loudly).
- **F9 (destroy-crash posture):** keep-closed is the documented safe posture
  (a half-destroyed class must not re-admit work); the `destroy_class`
  docstring states it. Pinned by test, no behavior change.
- **F10 (`__all__` completions):** `LingoFactory`/`CompositeClasses`
  exported from `materializer`; `SECRET_ENV_NAMES`/`CLIENT_TOKEN_PREFIX`
  exported from `config`.
- **F11 (residual lax ints):** `ApiConfig.port` and
  `EmbeddingConfig.max_tokens_per_batch` require genuine ints (no range
  check — a bad port still fails downstream-loud at bind; out of scope).
- **N2 (doc pointer):** `FlowToken.is_final` documents that production
  routing derives finality inline including `level == 1` (helper kept as-is,
  tests keep pinning it).

## Acceptance criteria (Slice 10)

- Cross-version `/deposits` → 409 `interface_mismatch`, nothing deposited;
  current-version and versionless (defaulted) deposits → 200; every proxy
  `put` stamps the current version on the wire.
- A crashing fact logs the exception with its task id and still raises; the
  pump fleet no longer shrinks silently in legio logs.
- Bool timeouts, `count < 1`, `pool < 0` fail loudly at their boundary.
- `ContractError` is a `LegioError`; tool load failure is an
  `UnrecoverableError`; CLI maps builtins to `legio error:` exits.
- Failed node-op submit leaves no stranded intent and propagates.
- Duplicate cache names fail naming both files; mid-destroy crash keeps the
  gate closed (pinned); new exports importable; bool/str/float ports and
  float batch sizes refused.
- Full suite + ruff + pyright green; no other behavior changed.

## Slice 11 contract (APPROVED)

Fifth-audit hardening from the Session 111 re-audit (0 blocking, 0 major).
Decisions first (scope control), then fixes:

- **M1 (unwired middleware, overbroad predicate):** `AuthMiddleware` stays
  the pure, tested decision unit and consumer hook (LEG-017 tests pin it;
  deleting it would destroy specified tested behavior) — it is NOT wired
  into production (inline enforcement in `api.py` stays canonical; wiring
  it is a bigger refactor, out of scope). The predicate is fixed to the
  explicit ARCH §10 federation-path set (exact `/catalog`, `/deposits`,
  `/health`, `/outbox` + prefixes `/work-items/`, `/outbox/`) so a
  `METHOD /submit`-shaped string no longer reads as federation. The
  package + ARCH wording is amended to the truth (decision helper, inline
  enforcement). Existing LEG-017 endpoint lists keep passing unchanged.
- **M2 (yaml/config parity):** `config._read_yaml` catches
  `UnicodeDecodeError` too, wrapping it in `ConfigError` like the CLI
  collector (Slice 9 F11 parity) — a bad-bytes config fails loud, never
  as a builtin traceback.
- **M3 (non-callable tool shape):** a resolved-but-non-callable tool raises
  `UnrecoverableError` (fatal authoring shape), not bare `TypeError` —
  inside the Slice 10 F6 taxonomy and the CLI builtin map.
- **M4 (federation taxonomy):** `InterfaceMismatchError` and
  `UnresolvableAgentError` derive from `UnrecoverableError` (fatal
  authoring/config), not bare `LegioError`. Every `except LegioError`
  site still catches (narrowing is behavior-safe); resolver tests keep
  pinning the raise sites.
- **M5 (bool pool/count residual):** `create_class(pool)` and
  `create_instance(count)` require genuine ints at verb entry
  (`type(x) is int`, Slice 9 F3 discipline) — `True` no longer passes as
  a live instance, `False` no longer passes as disabled.
- **M6 (retries bool residual):** `_tool_policy` rejects a boolean
  `retries` loudly, naming the tool (parity with the timeout guard and
  the file-path `_reject_bool_policy`).
- **M7 (leg order uniformity):** version-before-served on BOTH legs —
  work-items aligns to the deposits (Slice 10 F1) order. Interface
  compatibility is envelope-level and author-actionable; a stale peer's
  roster view may itself be stale, so 404 could mislead — 409 tells it
  to upgrade first. A validation-order note is added to the work-items
  endpoint (parity with deposits). Existing 404/409 tests (served-agent
  stale, current-version unknown) stay green.
- **M8 (clear logging):** `Runtime._clear_queue` logs the drained inbox
  (`class=` + `items=`, INFO) like its result-queue sibling (rule 11).

## Acceptance criteria (Slice 11)

- `POST /submit`-shaped strings are not federation endpoints; all LEG-017
  endpoint lists behave exactly as before.
- Bad-bytes configs fail as `ConfigError`; non-callable tools surface as
  `UnrecoverableError` (no `TypeError` in the payload); both federation
  errors are `UnrecoverableError`s.
- `pool=True/False` and `count=True` fail loudly at verb entry;
  `retries=False` fails loudly naming the tool.
- Stale-version + unknown-agent on work-items → 409
  `interface_mismatch` (uniform with deposits); nothing deposited.
- Destroying a class with queued items logs the inbox clear with the
  class and count.
- Full suite + ruff + pyright green; no other behavior changed.

## Slice 12 contract (APPROVED)

Sixth-audit hardening from the Session 113 re-audit. M2 is WITHDRAWN
(counting proof, Session 118: per-deposit kicks + destructive single-pop
+ eternal pumps make poison-stranding impossible through in-repo paths —
nothing to implement); M4(a)/(b) stay withdrawn (loud-at-use, unproven
harm; wiring the validator would add a patterns→naming edge). Decisions
first (scope control), then fixes:

- **M1 (uniform pattern policy, all types):** every pattern may declare
  `policy: {timeout}` (`AgentPolicy`, Schema 1/2 — NOT tool.policy,
  which stays the per-attempt tool-invocation bound). Authorship split:
  capacity = operator (pools, tools.yaml), step nature = pattern author.
  `AgentPolicy` forbids extras (a `retries` copy-paste from tool policy
  fails fast — the schema itself keeps the two policies distinct);
  `timeout` must be a genuine number, finite, `> 0`, else the whole
  load is refused naming file+pattern. Absent/empty → `None`
  (unbounded, today's behavior — zero regression) + a boot WARNING per
  unbounded pattern (visible, pinned). Enforcement is single and
  common: `AgentBase._run_guarded` wraps `_handle` in `wait_for`;
  `TimeoutError` flows into the existing error-result path unchanged.
  Constructors take `execution_timeout` (validated once in
  `AgentBase.__init__`); the materializer passes `spec.policy.timeout`
  for all three types; `ToolAgent` keeps its inner tool-policy layer —
  outer step / inner attempt, documented, NO cross-validation v1 (the
  loader never sees tool declarations; a misconfigured pair still fails
  loudly at runtime). For composites the bound covers each tick's
  handling; cross-tick flow deadlines are explicitly out of scope.
  `gather_budget` is untouched (mechanical wait-slice cap, ARCH §3,
  Slice 9 — not an execution bound; removal would need its own spec).
- **M3 (seed-envelope reads):** `read_outbox`/`status` branch on the
  generic envelope field `record.name == SEED_TASK` (the Runtime's own
  constant) — unknown/non-seed reads empty/`unknown task` exactly as
  today for genuinely-unknown ids. `kwargs` shapes are read only
  post-match (safe by construction: the same component built them at
  submit). No new stores (no dual-write), no Manager changes, no
  migration (pre-release).
- **M4(c) (loader YAMLError wrap):** `_load_all_documents` wraps
  `yaml.YAMLError → UnrecoverableError` with a source label (the
  loader's documented contract; both imports already present) —
  dir-walk passes the file, text input passes `"inline"`.
  `OSError`/`UnicodeDecodeError` already ride the CLI map; names and
  empty branches stay as-is per the withdrawals.
- **m1:** `DepositRequest.priority` rejects bools and non-finite values
  (`mode="before"`, genuine number).
- **m2:** string rejection joins the bool rejectors on
  `LifecycleParams` budgets and `ToolPolicy` (messages extended; no
  test pins the old ones); `schema_version` on both request models
  requires genuine ints (Slice 11 F11 discipline).
- **m3:** `_as_int` accepts only genuine ints (`None` → default,
  else loud `ValueError`) — no test uses the coercion.
- **m4:** token compares use `hmac.compare_digest` (utf-8 bytes, total
  function — no new exceptions on any input).
- **m5:** cross-consumer duplicate token secrets are refused loudly
  (naming the holder); same-consumer re-register stays legal;
  revoking an absent id logs a distinct noop (no false "revoked").
- **m6:** `key=value` on the naming guard and the catalog
  `unauthorized`/`no_capacity` events (non-secret fields only).
- **m7:** log the four unlogged raise points (local-hit resolve,
  non-callable raise, verb-shape rejects, unknown result origin),
  levels matched to their neighbors.
- **m8:** `_pending_controls` is capped (1024 entries — orders above
  any sane outstanding-control count; pools are small ints):
  over-cap evicts oldest-first with a visible warning (a late report
  then takes the existing orphan path — safe degradation).
- **m9:** manager success logs carry the result summary (type +
  length-or-name), never values (rules 7/11: log decisions, not data).
- **m10:** mount `GET /health` (`{"status": "ok"}`, L1 bearer like every
  ARCH §10 federation endpoint, no catalog required — health must
  answer especially with nothing configured). The predicate entry
  becomes true; ARCH already promises the route, so no doc change.

## Acceptance criteria (Slice 12)

- Any pattern of any type bounds its step handling when declaring
  `policy.timeout`; hung-LLM probe fails fast with `TimeoutError`;
  patterns without policy behave exactly as today (plus one boot
  WARNING each); `retries` under pattern policy fails the load.
- Internal task ids read empty/`unknown task` on outbox+status (no
  500); seed lifecycle + kick-on-miss unchanged; zero unguarded
  `kwargs` shape reads in `runtime` (grep-verified).
- Bad YAML fails as `UnrecoverableError` (file named for dir loads)
  on boot and create-class paths.
- Bool/NaN/inf priority, string budgets/policy, bool/str versions,
  string CLI ints, `==` token compares, duplicate secrets, silent
  revoke, bare log lines, unlogged raises, unbounded ledger,
  value-logging, missing `/health` — all closed per above.
- Full suite + ruff + pyright green; no other behavior changed.

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

## Slice 13 contract (APPROVED)

Seventh-audit hardening from the Session 121 re-audit (0 blocking).
Decisions first (scope control), then fixes:

- **M1 (Registry O(N) instance scan):** `_effective_class_state`
  (`registry:349-353`) scans all instances via `async for` with prefix
  match on every instance-related call. Fix: add a secondary index
  scope `instances_by_class` (`class_name → set[instance_id]`) updated
  atomically on `create_instance` / `destroy_instance` /
  `record_instance` / `remove_instance`. `_effective_class_state` becomes
  O(1) lookup; API `class_state` unchanged (public/private split
  preserved). No migration needed (index built lazily on first write).
- **M2 (Composite pending O(N) count):** `_has_pending`
  (`composite_agent:241`) calls `count()` on the state dict per
  collection cycle. Fix: maintain an integer `_pending_count` on the
  composite instance, incremented on fan-out (slot write) and
  decremented on fan-in (slot consume). `_has_pending` becomes
  `return self._pending_count > 0` — O(1), no beaver round-trip. The
  counter is authoritative; beaver state remains the source of truth for
  crash recovery (ledger is rebuilt on boot if needed — v1 keeps beaver
  as source, counter as cache; mismatch is impossible by construction
  since fan-out/join are the only writers).
- **M3 (NodeDB proxy safe surface):** `NodeDB.__getattr__`
  (`federation.py:371-372`) delegates `close()` and all other methods
  to the underlying `AsyncBeaverDB`. Fix: replace `__getattr__` with
  explicit delegation of only `queue`, `dict`, `lock` (the three
  primitives the ARCH §9 proxy needs). `close()`, `ensure_client`,
  internal attrs raise `AttributeError`. The proxy is a **queue router**,
  not a substrate lifecycle handle — agents must never close the shared
  handle.
- **m1 (Token registry linear scan):** `ClientTokenStore.resolve_consumer_id`
  (`security:79-82`) linear scans all tokens. Fix: add a reverse index
  `token → consumer_id` (dict) updated on `register`/`revoke`. Lookup
  becomes O(1). Token sets are small but unbounded growth path removed.
- **m2 (Pending-controls eviction orphans seq):** `_pending_controls`
  eviction (`runtime:375-379`) deletes the ledger entry but leaves
  `_control_sequence` entry intact. Fix: on eviction, delete the
  corresponding sequence entry too. A late report then correctly takes
  the orphan path (not a stale-seq false positive).
- **m3 (NodeDB client cleanup gap):** `NodeDB.ensure_client` lazily
  creates an `httpx.AsyncClient` (`federation:375-378`); `close()` not
  proxied. Fix: add `NodeDB.aclose()` that closes the owned client (if
  any); document that callers must `aclose()` the proxy (boot code path
  already tears down correctly; this hardens the contract).
- **m4 (Agent step error missing key=value):** `AgentBase._process_inbox_item`
  (`base:428`) emits WARNING without `key=value`. Fix: add
  `agent=`, `task=`, `error=` fields.
- **m5 (Cancelled step missing key=value):** `AgentBase._run_guarded`
  (`base:456-459`) catches `CancelledError` → re-raises without log
  event. Fix: emit INFO `key=value` on cancel before re-raise.
- **m6 (Proxy surface wider than needed):** `NodeDB.__getattr__` exposes
  `lock`, `dict` in addition to `queue`. Fix: explicit delegation list
  (same as M3) — only `queue` is actually used by ARCH §9.
- **m7 (Kick race narrow window):** `_kick_result_drain`
  (`runtime:610-618`) adds to `_drain_inflight` then submits; a
  `CancelledError` between add and `try` would leave flag set. Fix:
  move the add inside the `try` (add → submit → `finally` discard on
  submit failure) — flag is always removed on submit failure; cancel
  during add is impossible (no await between add and try).
- **m8 (Destroy TTL order):** `destroy_class` (`runtime:1355-1365`)
  cancels the control TTL key (`_control.pop`) before awaiting the
  destroy fact — if the fact fails after cancel, the control entry is
  gone and a late report would orphan. Fix: reorder to match Slice 9 F5
  (validate-first): await the fact first, then cancel TTL on success.
- **m9 (is_final level-blind doc):** `FlowToken.is_final`
  (`flow/token:22-28`) documents position-only finality; production
  routing requires `level == 1` AND end-of-sequence. Docstring already
  notes this (Slice 10 N2); no code change, just confirm doc accuracy.
- **m10 (Seed-record token validation gap):** `read_outbox`
  (`runtime:1056`) validates `record.name == SEED_TASK` but then
  `FlowToken.model_validate(record.kwargs["token"])` — a crafted seed
  record without `token` could 500. Fix: after envelope check, validate
  required kwargs keys (`token`, `client_id`) present; missing →
  `None`/`unknown task` consistent with envelope contract.
- **m11 (Defensive client_id check):** `status` (`runtime:1088-1090`)
  uses `.get("client_id")` — missing key returns `None`, denying even
  owner if record corrupt. Fix: use `in` check (`"client_id" in record.kwargs`)
  so missing key is distinguishable from mismatch.

## Acceptance criteria (Slice 13)

- `class_state` on a node with 10k instances across 100 classes is O(1)
  (wall-clock < 1 ms cold); no scan loop in profiler.
- Composite with 500 pending branches: `_has_pending` is O(1) (no
  `count()` round-trip in profiler).
- `NodeDB` proxy has no `close`/`ensure_client`/`lock`/`dict`
  attributes; attempting access raises `AttributeError`; `queue` works
  identically.
- `resolve_consumer_id` is O(1) (dict lookup) on 10k tokens.
- Evicted pending control has its seq removed; late report takes orphan
  path correctly.
- `NodeDB.aclose()` closes owned client; proxy used as async context
  manager in boot path.
- All four raise points carry `key=value` logs (`agent=`, `task=`,
  `error=`, `verb=`, `origin=`).
- Kick flag added inside `try`; cancel during add impossible.
- Destroy order: fact awaited, then TTL cancelled.
- `is_final` docstring unchanged (level-blind by design; note verified).
- `read_outbox` validates `token`/`client_id` keys post-envelope;
  missing → `None`.
- `status` uses `in` for `client_id` check; missing key → unknown task.
- Full suite + ruff + pyright green; no other behavior changed.
