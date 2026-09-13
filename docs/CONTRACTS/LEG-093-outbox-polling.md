# LEG-093 — Outbox polling by the author (write-before-ack, idempotency)

- **Status:** spec approved (maintainer, session 85q — federation-first reprioritization).
- **Rasante:** R-9
- **GitHub issue:** #46
- **Source:** `docs/PLAN.md` (LEG-093)
- **Depends on:** LEG-092 (the acceptor's work-item deposit), LEG-017 (L1 token).

## Goal

Result readback for remote work: the author polls the acceptor's outbox for the
result of a deposited work item; ack consumes (read-after-ack = empty). The
deposit is idempotent: a duplicate work item with the same id is never executed
twice.

## Problem (modern-triangle mapping)

The DRAFT referenced LEG-015 in-memory `Outbox` and `_queues`. In the modern
architecture the acceptor's outbox **is** the per-task result queue
(`result_queue_key(task_id)`) — the same queue the agent's flow already
deposits the `ExecutionResultMessage` onto at the end of level 1
(ARCHITECTURE §3, `_route_outcome` at `_deposit_result`). The write happens
**during** flow execution; the ack is a separate consumer step. This gives
"write-before-ack" **for free** — the result is always on the outbox before
any ack can occur; no helper or explicit ordering is needed.

Author-minted task ids (LEG-092) mean the result queue is keyed by the
author's own `<node_id>:<uuid>`. The author polls the acceptor's outbox by
task id; the shared L1 bearer token is the access guard (symmetric trusted
federation, ARCH §9). Ownership is implicit: a peer only polls its own task
ids.

Idempotency (a duplicate `POST /work-items/{agent}` with the same `task_id`
is never executed twice) is structurally guaranteed by the Manager's visible
"duplicate task_id" rejection (rule 9): the second deposit is refused before
a second seed is ever minted (LEG-092).

## Scope

- **In scope:** `GET /outbox/{task_id}` (non-blocking poll, L1 guard), `DELETE
  /outbox/{task_id}` (destructive ack, L1 guard), the Runtime seams
  `read_outbox`/`ack_outbox` that wrap the result queue, and the idempotency
  guarantee at the queue-message level.
- **Out of scope:** the author-side HTTP client (the acceptance is the
  endpoint; the authoring client is exercised by LEG-094); the result write
  itself (the agent flow handles it).

## Contract & design

- `create_app(..., federation_token=...)`: when a federation token is provided
  the app also mounts `GET /outbox/{task_id}` and `DELETE /outbox/{task_id}`
  alongside `GET /catalog` and `POST /work-items/{agent}`; absent → 404.
- `GET /outbox/{task_id}` (L1 guard, no owner scoping — symmetric trust):
  - Result not yet deposited → **200** `{id, ready: false, result: null}`
    (non-blocking poll; the author polls until `ready` flips — rule 8).
  - Result present → **200** `{id, ready: true, result: {...}}` (the
    `ExecutionResultMessage` payload).
- `DELETE /outbox/{task_id}` (L1 guard):
  - Something consumed → **200** `{id, acked: true}` (destructive drain of
    `result_queue_key`).
  - Queue already empty → **200** `{id, acked: false}` (idempotent ack, no
    error).
- `Runtime.read_outbox(task_id)` — non-destructive peek of
  `result_queue_key(task_id)`. Returns the result dict or `None`.
- `Runtime.ack_outbox(task_id)` — destructive get from
  `result_queue_key(task_id)`. Returns `True` if something was consumed.
- Write-before-ack is structural (not an API rule): the agent's
  `_deposit_result` writes the `ExecutionResultMessage` to `end_of_level_queue`
  during execution; the ack is a separate consumer step. A crash after ack
  never loses a result that was never written.
- Lifecycle stays local (maintainer session 85q): the outbox surface never
  accepts an operator verb.

## Interface

- `GET /outbox/{task_id}` → `200 {id, ready, result}` (no result → `ready:
  false`, `result: null`; result present → `ready: true`, `result: {…}`).
- `DELETE /outbox/{task_id}` → `200 {id, acked}`.
- `401` no token / wrong token (L1).
- `404` absent (no federation configured).
- `422` invalid task_id.

## Acceptance criteria

From `docs/PLAN.md` (LEG-093), verbatim:
- Author polls outbox, ack consumes; re-read after ack is empty; duplicate
  work-item (same id) is not executed twice.

## Tests (red-first)

- Poll on a task_id with no result deposited → `ready: false`.
- After depositing an `ExecutionResultMessage` on the task's result queue → poll
  → `ready: true` with payload.
- Ack consumes: DELETE → `acked: true`; re-poll → `ready: false`.
- Idempotent ack: DELETE on an empty outbox → `acked: false`.
- Write-before-ack pin: the result is readable (GET → `ready: true`) before any
  ack has occurred (the ack never triggers the write).
- Duplicate work item: POST the same work item twice → pump the manager → only
  **one** `ExecutionRequestMessage` on the class queue (idempotency at the
  queue-message level, the "not executed twice" guarantee).
- 401 no/wrong token; 404 unconfigured; 422 invalid task_id.
- Lifecycle stays local: the outbox surface never accepts a lifecycle verb.

## Validation case

- LEG-094 multi-node example (author deposits a work item on the acceptor,
  polls the outbox, acks).

## Definition of done

- All acceptance criteria met by running checks (ruff + pytest + pyright green).
- Maintainer approval recorded; the maintainer closes the GitHub issue.
- Journal entry appended; the LEG-093 work tree commits when stable.
