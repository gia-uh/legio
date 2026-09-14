# LEG-088 — Node control intake (`node_ops`) + Runtime wiring

Status: spec approved (maintainer, session 85m).
Issue: `docs/PLAN.md` R-8, LEG-088.
Depends on: LEG-082 (control channel), LEG-087 (lifecycle facts minting control).

## Problem

Lifecycle orders are minted **only** by the node's Runtime. Their ultimate
source is an operator (CLI, HTTP/API, a peer) or the node's own policies
(`origin: operator | automatic`). That source must reach the Runtime through a
decoupled intake — the Runtime's own queue, drained **via Manager facts**, never
pumped by the Runtime — so the operator surface never touches agent queues and
never mints a message.

## Scope

### A. The `node_ops` intake queue

- A new Runtime-owned beaver scope: `db.queue("node_ops")`. This **extends** the
  Runtime's footprint from "only `gates`" to "`gates` + `node_ops`" (rule 13;
  naming deliberately distinct from the Manager's `control` scope — the Manager
  controls *tasks*, `node_ops` carries *operator intents*).
- Intents are small, typed payloads (e.g. `{verb: enable_instance, class: ...,
  instance: ...}`), signed or not by the external caller — the Runtime decides
  which intents a caller may deposit through it (the CLI/API layer is LEG-081).

### B. Runtime → Manager wiring

- The Runtime drains `node_ops` **through a Manager fact** (e.g. a
  `NODE_OP` polling fact that owns the intake): each intent is validated by the
  Runtime's decision logic, then dispatched to the corresponding lifecycle fact
  (LEG-087), which mints and deposits the signed `ControlMessage`. The Runtime
  keeps deciding; the Manager keeps executing; the Registry keeps mirroring
  posteriori.
- Rule 8 holds: the intake is a drain loop with a scheduling **field**, never a
  sleep; the agent's waiting is the sanctioned queue suspension (LEG-082).

### C. Tests (red-first)

- An intent on `node_ops` reaches the target agent as a signed control message
  honored between dispatches; the operator never touches the agent queue;
  footprint pin: the Runtime owns exactly `gates` + `node_ops`
  *(extended by LEG-095 to + `state_report`, and by LEG-095 Phase 2 to
  + `outbox` — session 86b).*
- Unknown/invalid intents surface visibly (rule 9) and never mint anything.

## Non-goals

- The CLI/HTTP surface that deposits intents (LEG-081).
- The `automatic` origin policies (nothing in the engine invents one).

## Notes / debt

- `AGENT_LIFECYCLE.md` §6.1 footprint table is amended to the `node_ops` line.
- Implementation (session 85o): intents are the typed `NodeOp` payload
  (`{verb, class_name, instance_id}` — the `class`/`instance` names of §A are
  mapped to pydantic-safe field names). `Runtime.deposit_node_op(...)` is the
  intake surface (returns the drain task id); each deposit queues the intent
  and schedules one `NODE_OP` drain.
- The `node_ops` drain (`_node_op_fact`) pops **one** intent per dispatch,
  relays it to the lifecycle verb, and **re-submits itself while the intake is
  non-empty**: the scheduling decision is the presence of work on the queue (a
  `count()` field read), never a sleep — rule 8. A drained intent that fails
  validation (e.g. a tampered unknown verb) surfaces visbly as a `failed`
  `NODE_OP` task and never mints anything (rule 9).
- Executor occupancy: a `NODE_OP` drain awaits the lifecycle confirm, so it
  occupies its executor for the lifetime of one intent — the intake depends on
  the §6.1 multi-executor topology (one executor per occupant + spares), the
  same LEG-087 doctrine a booted node already follows.