# LEG-082 — Authenticated control channel + standing agent loop

Status: spec approved (maintainer, session 85m).
Issue: `docs/PLAN.md` R-8, LEG-082.

## Problem

Running an agent is legio's responsibility; an agent is an in-process asyncio
task that runs **itself** (its own loop over its own class queue — autonomous
in the architectural sense, never in a separate process). The **only** way to
talk to an agent is its queue. Lifecycle orders (enable / disable /
terminate-with-drain) must therefore arrive as **authenticated messages** on the
agent's class queue — minted **only** by the node's Runtime, validated by the
agent itself, and honored **between dispatches** (never mid-step).

## Scope

### A. `legio.flow.control`

- `ControlAction(str, Enum)` — `ENABLE`, `DISABLE`, `TERMINATE_WITH_DRAIN`.
- `ControlOrigin(str, Enum)` — `OPERATOR`, `AUTOMATIC` (the lifespan decides the
  origin; nothing in the engine assumes one).
- `ControlMessage(frozen pydantic)` — `schema_version`, `message_type="control"`,
  `target_instance` (the instance id, never the class), `action`, `origin`,
  `seq` (int ≥ 1), `signature` (hex). `extra="forbid"`.
- `derive_control_key(secret_material) -> bytes` — HMAC key derivation from a
  boot secret, **in-process and never persisted**. Rotating the key per boot
  means a signature from a previous boot never validates after restart (replay
  across restarts is impossible without the key; within one boot the monotonic
  `seq` closes the window).
- `sign_control(key, *, target_instance, action, origin=-, seq) -> ControlMessage`.
- `ControlVerifier` — a **pure, verify-only** handle injected into an agent at
  materialization (LEG-087 wires the injection; it can verify, never mint).
  Verification is HMAC over the canonical message fields; a failed signature is
  a rejection, never a silent pass.
- `CONTROL_PRIORITY = -1.0` — control deposits use a lower priority than work
  (beaver `ORDER BY priority ASC`, work stays `0.0`), so control jumps work at
  the agent's next dispatch.

### B. `AgentBase.standing_loop()` (the interior of the agent's own loop)

- A **long-lived** loop that suspends on the class queue (beaver
  `get(block=True)` — the sanctioned "the agent suspends in its own queue"
  suspension; beaver's internal 0.1s producer-interleave yield is the queue's
  consumption mechanism, not an engine timer). The loop is the agent's life:
  it only ends when the agent decides to exit.
- An item whose `message_type == "control"` takes the control path; anything
  else keeps the `ExecutionRequestMessage` path (`_process_inbox_item`). The
  class inbox now carries control messages **as well as entries** (§12.2
  amended); results still partition by queue (never onto an inbox).
- Control handling (all **between dispatches**, never mid-step):
  1. `ControlVerifier is None` → visible `WARNING` (rule 9), item consumed and
     dropped — never silently ignored.
  2. Signature/target/seq validation fails → visible `WARNING`, item consumed
     and dropped — never silently ignored. `seq` is monotonic per instance;
     `seq <= last seen` is a replay → rejected.
  3. `target_instance != self` → **requeued at the back** of the class queue
     (instance-addressed control over a shared pool queue). A puller that keeps
     racing a foreign control is accepted best-effort, mirroring the §12.5.4
     race-at-the-threshold decision; the message is never ack-dropped.
  4. `ENABLE` → resume consuming work (`_control_paused = False`).
  5. `DISABLE` → park retrieving work: the loop stays alive, keeps the queue
     drained of control, and **requeues work items at the back**; the agent is
     alive (its record reads `running`) yet consumes nothing.
  6. `TERMINATE_WITH_DRAIN` → finish any in-flight work, then **return** (the
     loop ends; the agent exited its own loop — the structural "death" the
     Manager observes).
- `run()` / `process_next()` are unchanged (the ephemeral drain loop; used by
  legacy/verification paths and by tests).
- A composite (two-inlet intake, §12.3) integrates the same control handling on
  its intake inlet; its standing loop reuses the control path.

### C. Tests (red-first)

1. Sign/verify round-trip; tampered field, wrong target, wrong key, stale
   `seq`, forged `signature` all **rejected**.
2. `standing_loop`: work honored only when verified control allows; `disable`
   parks (work requeued, alive), `enable` resumes; `terminate_with_drain` ends
   the loop after in-flight work; foreign-target control requeued; no-verifier
   and invalid control → `WARNING` + dropped, loop continues.
3. Priority: a control deposited after a work item is consumed first.

## Non-goals

- The real bring-up registration (async generator, Manager record semantics) is
  LEG-087.
- The `node_ops` intake queue (operator source) is LEG-088.
- The CLI surface is LEG-081.
- No new dependencies (`hmac`/`hashlib` stdlib). Domain-free (rule 7). Logging
  ships with the implementation (rule 11). Errors never silent (rule 9).