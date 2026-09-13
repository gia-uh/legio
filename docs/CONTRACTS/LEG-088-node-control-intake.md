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
  footprint pin: the Runtime owns exactly `gates` + `node_ops`.
- Unknown/invalid intents surface visibly (rule 9) and never mint anything.

## Non-goals

- The CLI/HTTP surface that deposits intents (LEG-081).
- The `automatic` origin policies (nothing in the engine invents one).

## Notes / debt

- `AGENT_LIFECYCLE.md` §6.1 footprint table is amended to the `node_ops` line.