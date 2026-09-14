# LEG-087 — Real bring-up (parked async generator) + lifecycle facts minting control

Status: spec approved (maintainer, session 85m).
Issue: `docs/PLAN.md` R-8, LEG-087.
Depends on: LEG-082 (the standing loop + `legio.flow.control`).

## Problem

The bring-up fact is still the one-shot `return instance_id`
(src/legio/runtime/__init__.py:132): nothing actually runs an agent. Real
bring-up must (a) start the instance's own loop (LEG-082 `standing_loop`), and
(b) let the Manager know **structurally** — while the agent lives the record
reads `running`; the moment the agent exits its own loop, the record reaches a
terminal state. No timers, no polls: the record's terminal is the agent's own
exit, observed as the callable returning.

Instance life verbs (enable / disable / destroy) must no longer couple the
instance to Manager control-mode (`pause`/`resume`/`cancel` of the vehicle).
They are **orders to the agent**, delivered as signed `ControlMessage`s on its
own queue (LEG-082) — minted only by the Runtime's lifecycle facts, confirmed by
bounded Manager reads (§5.8 rule-8 exception).

## Scope

### A. The real bring-up as a parked async generator

The boot registers `BRING_UP_TASK` with an **async generator** (the seam the
one-shot leaves open):

- Body: `agent = agent_map[class_name]`; `loop_task = asyncio.create_task(
  agent.standing_loop(instance_id))`; then `try: yield instance_id; while True:
  yield (checkpoints)` `finally: await loop_task`.
- The Manager parks it (`_drive_parked`, §6.1 "async generator"): control of the
  **fact task** is cooperative at yields; `pause` parks it; `cancel` → `aclose()`
  → `GeneratorExit` at the yield → the `finally` awaits the agent's cooperative
  exit before the record is written `failed(cancelled)`.
- Record semantics: **while the generator is parked the record reads `running`**;
  when the agent exits its own loop (its `terminate_with_drain` honored), the
  generator's `finally` returns and the next drive raises `StopAsyncIteration` →
  the record is written `success` — **the agent's death and the record's terminal
  are the same structural moment**, never a timer or a poll.
- One executor per life-task (one parked generator per instance) — the §6.1
  multi-executor is already sanctioned.
- The bring-up **confirm changes**: it waits for the record to read `running`
  (§5.1 step 2), not `success` as today.

### B. Lifecycle facts mint the control message

New Manager facts registered at boot (the Runtime decides, the Manager executes):

- `ENABLE_INSTANCE` / `DISABLE_INSTANCE`: the fact mints a signed
  `ControlMessage(action=enable|disable, origin=operator)` with the per-boot node
  key, registers the mint in a pending ledger, and deposits it at
  `CONTROL_PRIORITY` on the target instance's class queue.
  Confirm: bounded Registry read that the instance row reaches the target state
  (§5.8), **converged via the agent's state report** — one report per honored
  control, applied by the Runtime's report intake only after correlating
  `(instance_id, action, seq)` with the pending mint (LEG-095). There is
  deliberately **no optimistic write**; the Registry is updated posteriori.
- `DESTROY_INSTANCE`: mints `ControlMessage(action=terminate_with_drain)` and
  deposits it; confirm: bounded wait for the **bring-up** record to reach
  `success` (the agent drained and exited its own loop; the generator ended).
  `manager.cancel` is no longer the destroy path — the message ends the agent,
  and the record terminal is its structural consequence. A no-vehicle row
  (reboot) stays a pure Registry fact removal (§4.8 legacy).
- Class enable/disable stay as today (policy A: a disabled class keeps draining;
  instances keep running; the class gate closes inflow).

### C. Boot wiring (materializer)

`boot_node` derives the node-local control key, computes the **verify-only**
handles, injects them into the materialized agents (LEG-082 param), registers
the real bring-up generator and the three lifecycle facts, and registers the
per-agent verifier with `standing_loop`'s instance binding, all fail-fast (rule 9).

### D. Tests (red-first)

- Bring-up confirm by `running`; the record stays `running` while the agent
  lives; `terminate_with_drain` → record `success` (drain-first: in-flight
  deposits before the exit). `manager.cancel` **mid-life is not a supported
  path** (see re-scoping note below) — the record's structural terminal is the
  agent's own cooperative exit, never a cancel.
- Enable/disable/destroy verbs: message minted+signed, deposited on the target
  queue at control priority, honored by the agent, Registry updated posteriori.
- Old Manager control-mode coupling removed from the instance verbs; existing
  runtime lifecycle tests updated to the message model.
- Full suite + ruff + pyright green.

> **§D re-scoped by the maintainer (session 85m).** The approved spec's
> `manager.cancel mid-life → aclose → failed(cancelled)` test is **removed**: the
> Manager's `cancel` is never the destroy path — the Runtime mints and deposits
> `terminate_with_drain`, and the bring-up record's terminal is its structural
> consequence (`success`). The one-shot conversation also established
> (empirically) that `aclose()` on a generator suspended at an in-flight
> `__anext__` awaiting the standing loop raises "asynchronous generator is
> already running"; the real bring-up never relies on `aclose`, its `finally`
> cancels `loop_task` when not done (a manager-level `cancel` while parked at the
> first yield still strands nothing).

## Non-goals

- The `node_ops` intake (the `origin: operator` source) is LEG-088; the CLI is
  LEG-081.
- The standing loop interior itself is LEG-082 (already in).

## Notes / debt

- Enable/disable converge via the **state report** (LEG-095): the agent
  acknowledges a control by depositing one report per honored control; the
  Runtime correlates it against its pending mint and applies it. The confirm is
  a bounded Registry read. Documented, not debt.
- **Executor occupancy:** a real bring-up's parked generator occupies its
  dispatching executor for the agent's whole life. A node therefore runs **one
  executor per live bring-up instance + spares for facts** (§6.1).
- **Debt — `pool > 1`:** the mounted agent map holds **one agent object per
  class**; two concurrent standing loops of the same class would share that
  object's per-instance control state (`_control_instance`,
  `_paused_hold`, `_last_control_seq`). Not handled: pool>1 real bring-up with
  live control is unsupported until instances get per-instance control state or
  per-instance agent objects. A visible `WARNING` when a second live loop spawns
  on a shared agent is the minimum guard.
- `AGENT_LIFECYCLE.md` §5.1 step 2 confirm, §6.1 bring-up row, and the §8 step-4
  wording are amended to the parked-generator model; the §5.3–§5.8 verbs are
  amended to the message model (enable/disable further amended to the
  report-convergent model by LEG-095).