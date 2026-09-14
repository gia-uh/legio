# LEG-095 — Instance state report (the control channel's out-side)

- **Status:** Phase 1 APPROVED (maintainer, session 86b — "haz los pasos 1 y 2");
  Phase 2 spec below APPROVED in-session (same turn)
- **Rasante:** R-8 (amends LEG-082 / LEG-087; Phase 2 amends LEG-011 / LEG-050 /
  LEG-053 / LEG-085 / LEG-093)
- **GitHub issue:** #50
- **Source:** session 85x/85y (maintainer-led analysis) + `docs/PLAN.md` (R-8)
- **Depends on:** LEG-082 (authenticated control channel + standing loop), LEG-087
  (real bring-up + lifecycle facts minting control), LEG-088 (`node_ops` intake
  pattern), LEG-085 (runtime public face), LEG-093-independent (outbox is a later
  phase; not touched here)

## Goal

Give the Runtime **real knowledge** of whether an instance honored a lifecycle
order. Today `enable_instance`/`disable_instance` mint and deposit a signed
`ControlMessage`, weakly confirm *liveness* (`_confirm_vehicle_alive`), then
write the Registry **optimistic, a posteriori** (LEG-087:55-56 "deliberately no
ack"). The agent's only voice is a queue deposit; the control path does (1) read
a queue, (2) process, but **(3) writes nothing** — the out-side of the
`read → process → write` contract is missing. This issue adds it: the agent
deposits a **state report** after honoring, a Runtime intake consumes it, and
only then is the Registry/gate state written — a posteriori with real knowledge.

## Scope

- **In scope:** the report envelope; the agent's deposit on honor; the Runtime
  intake fact; the confirm of `enable`/`disable` becoming report-convergent; the
  removal of the "no ack" clauses in LEG-087 and AGENT_LIFECYCLE §5.3/§5.5-§5.6.
- **Out of scope:** per-submit-result queues + author outbox (Phase 2, separate
  issue); the db proxy / federated deposit (Phase 3, separate issue); pool>1
  control state (documented debt, LEG-087).

## Contract & design

### A. The report envelope (`legio.flow.control`)

`AgentStateReport` (frozen pydantic, `extra="forbid"`): `schema_version`,
`message_type = "state_report"`, `instance_id` (the honored instance, never the
class), `action` (the honored `ControlAction`), `seq` (the honored control's
monotonic seq — correlation key), `state` (result after honoring:
`parked` after `disable`, `ready` after `enable`, `terminating` after
`terminate_with_drain`).

- **Unsigned by design.** The agent holds a verify-only handle and can never
  mint (LEG-082). The report is therefore **not signed**: the Runtime trusts it
  by **correlation with its own mint ledger** (the `seq`/`action`/`instance` it
  itself minted via `_control_sequence`). An external writer cannot mint a
  control in the first place; a report that does not correspond to a pending
  minted control is a visible `WARNING` and is ignored (rule 9), never silent,
  never applied.
- The intake is **node-internal**, one beaver queue by name
  (`STATE_REPORT_SCOPE = "state_report"`, plain beaver naming — exactly the
  `node_ops` pattern, LEG-088, not the `legio:queue:` family).

### B. Agent side: deposit on honor (`AgentBase._honor_control`)

`base.py:262` — after a control passes validation (verifier/schema/signature/
replay) and **is honored**, the agent deposits one `AgentStateReport` before
returning:

| Honored action | Resulting state | Deposit point |
|---|---|---|
| `disable` | `parked` | after `_control_paused = True` (base.py:319-326) |
| `enable` | `ready` | after `_release_hold()` (base.py:327-335) |
| `terminate_with_drain` | `terminating` | before the loop returns (base.py:336-344) |

- One report **per honored control**; a rejected/requeued/foreign control emits
  nothing. `seq` always the honored message's `seq`.
- The deposit is `await self._db.queue(STATE_REPORT_SCOPE).put(report, priority=0.0)`
  — a queue write through the agent's db handle (Phase 3 later routes local
  only; the intake is node-internal, always local). A failed deposit is an
  `logger.exception` (rule 9/11) and **does not change** the honor decision: the
  loop state already transitioned; on destroy the structural record is the
  confirm anyway.
- Composite: the shared `_honor_control` on the intake inlet reports uniformly.

### C. Runtime intake fact (`STATE_REPORT_TASK`, drains `state_report`)

Mirrors `_node_op_fact` (LEG-088 §B): a Manager fact registered at boot that
pops **one** report, validates, applies, and re-schedules while work remains
(scheduling field = presence on the intake, rule 8).

Validation (each miss = visible `WARNING`, item consumed, **never applied**):
1. Schema valid.
2. Instance exists (registry).
3. **Pending mint ledger hit:** a control with exactly `(instance_id, action,
   seq)` was minted by this Runtime and its report has not arrived yet. The
   ledger keeps a pending entry from mint until the matching report is applied.

Apply (real knowledge, no optimism):
- `state = ready` → `registry.set_instance_state(instance, ENABLED)`
- `state = parked` → `registry.set_instance_state(instance, DISABLED)`
- `state = terminating` → informational: the bring-up record's `success` is the
  structural destroy confirm (unchanged); applied for the log/ledger only.

### D. Verb confirm becomes report-convergent

`enable_instance`/`disable_instance` (runtime:1087-1137) drop the optimistic
`set_instance_state`. New flow:

1. (unchanged) no-op guard on a state already equal to the target.
2. Mint + deposit the `ControlMessage` via the lifecycle fact; the mint also
   enters the **pending** ledger entry.
3. Bounded report-convergence wait: read-poll the Registry until the instance
   state equals the target (mirrors `_await_observable_state`, runtime:510-535,
   the sanctioned §5.8 bounded clock wait over the class lifecycle budget). A
   `FAILED` bring-up record raises immediately with its error; exhausting the
   budget raises a visible `RecoverableError` ("no state report within Xs") —
   never a silent grey state.
4. On convergence: pending ledger entry consumed; `set_instance_state` has
   already been applied by the intake (single writer to the Registry state).

`destroy_instance` (runtime:1139-1182) is **unchanged**: `terminate_with_drain`
report is informational; the confirm stays the bring-up record reaching
`success`.

### E. Doc updates (approved with this contract)

- LEG-087: remove "There is deliberately no ack" (:55-56) and the
  "no strong ack ... by design" notes (:105-106); confirm wording becomes
  report-convergent.
- AGENT_LIFECYCLE: §5.3/§5.5-§5.6 "weak bounded read / deliberately no ack"
  wording (:875, :912, :1160-1161) → report-convergent; the verb tables
  (`disable_instance`/`enable_instance` rows) gain the report step.
- ARCH §7.1/§12 wording gains the state-report intake (node-internal queue
  family, next to `node_ops`).

## Interface

- `legio.flow.control`: `AgentStateReport`, `STATE_REPORT_MESSAGE_TYPE`,
  `ReportedState` (`parked | ready | terminating`).
- Naming: single node-internal intake queue `state_report` (plain beaver scope,
  like `node_ops`; not in the `legio:queue:` family).
- Runtime public face (LEG-085): new Manager fact `STATE_REPORT_TASK`; the
  `enable_instance`/`disable_instance` signatures are unchanged; their confirm
  semantics change from weak-liveness to report-convergent.
- Logging (rule 11): mint → pending ledger entry (INFO); report consumed with
  ledger hit → applied (INFO); each validation miss → `WARNING`; deposit
  failure → `logger.exception`; convergence timeout → `WARNING` +
  `RecoverableError`.

## Acceptance criteria

From `docs/PLAN.md` (LEG-095): the Runtime learns an instance honored
`enable`/`disable`/`terminate_with_drain` by **reading its own intake**, and the
Registry/gate state is written only after that report — no optimistic
posteriori writes remain on the instance verbs.

## Tests (red first)

1. Agent: honoring enable → `ready` report on `state_report` with the honored
   `seq`; disable → `parked`; terminate_with_drain → `terminating`. Rejected
   (no verifier / bad signature / replay / foreign target) → **no** report.
2. Intake: valid report for a pending mint → state applied; report with unknown
   instance / no pending mint / seq mismatch → `WARNING`, ignored, state
   unchanged.
3. Verbs: `disable_instance` returns only after `state_report` consumed and the
   Registry reads `disabled`; no state is written *before* the report; a
   missing report within `drain_timeout` → visible `RecoverableError`. Same for
   enable. `destroy_instance` confirm unchanged (bring-up `success`).
4. Keep `_confirm_vehicle_alive` semantics testable for the destroy path that
   still needs it; lifecycle full suite green.
5. Full suite + `ruff` + `pyright`.

## Validation case

- The runtime lifecycle example (create → disable one instance → Registry reads
  disabled only after the report, visible in `status`); plus an explicit
  no-report timeout surfacing as an error (never a silent state).

## Definition of done (Phase 1)

- All acceptance criteria met by running checks.
- Maintainer approval recorded (session 86b); the maintainer closes the GitHub
  issue (#50, maintainer opens).
- Journal entry appended.

---

## Phase 2 — Per-agent result queue + outbox intake (spec, approved in-session 86b)

- **Status:** spec APPROVED in-session by the maintainer (session 86b).
  A separate GitHub issue number is pending (maintainer opens, same as Phase 1).
- **Source:** session 85y roadmap item 2 ("Result queue per submit type, not per
  task") + session 86b derivation (kick sources, kick-on-miss, ack semantics).
- **Depends on:** LEG-011 (`end_of_level_queue`), LEG-050/053 (final-result
  delivery), LEG-085 (Runtime footprint), LEG-093 (outbox verbs), LEG-092
  (author-minted ids — the outbox record key).

### Goal

`result:<task_id>` creates one beaver queue per submit, never reclaimed. Phase 2
makes the final-result queue **per served root agent** — `result:<agent>`,
shared by every task that starts on that agent — and adds a Runtime intake
(`RESULT_DRAIN`) that consumes `ExecutionResultMessage`s into a per-task
`outbox` dict. `status` / `read_outbox` / `ack_outbox` read **records**, never
the physical queue.

### Contract & design

#### A. Naming (`legio.naming`)

- `result_queue_key(agent_name)` → `result:<agent>` — the final-result queue of
  one root agent. The submit seeds it as the level-1 `end_of_level_queue`; the
  flow code is untouched (it carries the name opaquely).
- `OUTBOX_SCOPE = "outbox"` — the Runtime-owned beaver dict holding one record
  per completed task, keyed by `task_id`. The record is the
  `ExecutionResultMessage` dump (self-describing, re-validated on read).
- `outbox_key(task_id)` → `outbox:<task_id>` — the informational pointer
  `status` reports as `result_key` once the record exists.

#### B. Intake fact (`RESULT_DRAIN_TASK = "result_drain"`, parameterized by agent)

Mirrors `_node_op_fact` / `_state_report_fact` (pop one, apply, re-schedule
while work remains — scheduling field = presence on the queue, rule 8):

1. Pop ONE item off `result:<agent>` (non-blocking). Empty → done, no re-kick.
2. Validate as `ExecutionResultMessage`. Miss → visible `WARNING`, item
   consumed, **never applied** (rule 9).
3. Hit → `outbox.set(task_id, dump)` (INFO). No gate check: a computed result
   must never strand on a queue whose class was disabled afterwards.
4. Re-kick with the same agent while items remain.

#### C. Kick sources (all scheduling, never pumping — the node owns the executor)

- `submit` / `submit_work_item` kick for the starting agent after the seed is
  minted (drains backlogs; cheap no-op otherwise).
- `status` / `read_outbox` kick **iff the task's record is absent**
  (kick-on-miss — bounds Manager-task growth during poll loops). The agent
  comes from the seed token's `launcher_class`; an unknown seed means no kick
  (`status` still raises `KeyError`, `read_outbox` still returns `None`).
- `ack_outbox` never kicks: it consumes the record only. A pure
  ack-without-poll on an uncollected result returns `False` (no silent loss —
  the next poll collects it).

#### D. Seam semantics

- `status`: ownership/`FAILED` checks unchanged; COMPLETED + `output` +
  `result_key = outbox:<task_id>` only when the record exists, else
  PENDING/RUNNING as today.
- `read_outbox`: record payload or `None` (non-blocking poll, rule 8).
- `ack_outbox`: fetch + delete → `True`; absent → `False` (idempotent, never
  an error).
- `destroy_class`: also clears `result:<name>` (drain-and-discard, INFO).
  Per-task outbox records survive (consumed by `ack`; `status` stays
  servable) — destroy ends the class, not the tasks' readable history.

#### E. Untouched

Agent flow (`_deposit_result`, gather queues, branch closes), the HTTP shells
(same endpoints/codes), Manager/Registry scopes. Runtime footprint extends to
`gates` + `node_ops` + `state_report` + `outbox` (ARCH §2/§7, LEG-085 amended).

### Acceptance criteria

- `submit` seeds `end_of_level_queue == result:<starting-agent>`; two submits
  on the same agent share one queue name.
- A flow-end result on `result:<agent>` is collected by the drain; `status`
  reports COMPLETED only via the record (never by peeking the queue).
- Two tasks on one agent → two records keyed by `task_id`; no cross-talk.
- An invalid item on a result queue → `WARNING`, consumed, never applied.
- `destroy_class` clears `result:<name>`; ack consumes; re-read empty;
  ack-on-empty `False`.

### Tests (red first)

- New `tests/test_leg095_result_drain.py` (submit seeds per-agent queue; drain
  collects → `status` COMPLETED via record; kick-on-read path; two-task
  isolation; invalid item consumed with WARNING; ack/empty semantics;
  destroy clears the agent's result queue; unknown task → no kick, `None`).
- Updated to the record model: `test_leg014_manager`, `test_leg026_e2e`,
  `test_leg032_example_summarize`, `test_leg043_examples_composites`,
  `test_leg044_nested_composites`, `test_leg081_boot`,
  `test_leg085_runtime`, `test_leg093_outbox`,
  `test_flow_integration_decoupled`.
- Full suite + `ruff` + `pyright`.

### Logging (rule 11, with the implementation)

- `submit` / `work_item` INFO carry `result_queue=result:<agent>`.
- Drain hit → INFO `runtime result_drain task=… agent=… recorded=true`;
  invalid item → WARNING; ack → INFO; destroy clearing → INFO.

### Validation case

- The `transform` E2E (LEG-026) over the record model: submit → seed → agent
  run → `status` kick → drain → `status` COMPLETED with `result_key =
  outbox:<task_id>`; plus the LEG-093 acceptor round trip
  (deposit → drain → poll → ack).