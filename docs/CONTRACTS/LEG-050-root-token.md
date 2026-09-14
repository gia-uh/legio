# LEG-050 — Submit seeding & final-result delivery (Schema 2)

- **Status:** DRAFT (awaiting maintainer approval; reworded 2026-08-31 to the
  Schema 2 submit model — no `results:{task_id}` store, no `client:{task_id}`)
- **Rasante:** R-5
- **GitHub issue:** #27
- **Source:** `docs/PLAN.md` (LEG-050); `docs/AGENT_LIFECYCLE.md` §4.11 Schema 2
- **Depends on:** LEG-014, LEG-011

## Goal
The **submit** seeds the flow: it places the first `ExecutionRequestMessage` on
a `main` class in **level 1** with `end_of_level_queue` = the **final-result
queue**, and the final result is delivered there exactly-once. There is no
`results:{task_id}` store and no `client:{task_id}` queue.

## Scope
- **In scope:** submit authoring semantics (level 1 + final-result queue),
  delivery into the final-result queue, ack of client delivery, no re-delivery.
- **Out of scope:** termination flow (LEG-014 owns it at manager level).

## Contract & design
- On submit: the flow is authored as LEG-011 (Schema 2): level `1`,
  `end_of_level_queue` = the **final-result queue** (the starting agent's
  shared `result:<agent>` queue, LEG-095 Phase 2; the `RESULT_DRAIN` intake
  collects it into the per-task outbox record the API reads back via
  `status`). The agent never chooses the
  destination; parallel-as-root passes its sequence as level 1
  with the same final-result queue and fan-in via its gathering queue.
- On **flow end** (end-of-sequence AND `level == 1`) the final
  result is delivered to that final-result queue; client delivery ack'ed so it
  happens exactly once (a duplicate/retry cannot double-write — idempotency
  guard).
- No re-delivery after ack.

## Interface
- Internal authoring/token API + ack handshake.

## Acceptance criteria
- A submitted task's final result lands in the final-result queue exactly once
  and is readable via `status`; no re-delivery after ack.

## Tests
- Contract tests (red first): exactly-once, ack no-redelivery.

## Validation case
- E2E `transform` example asserting the final-result queue.

## Definition of done
- All acceptance criteria met by running checks.
- Maintainer approval recorded; the maintainer closes the GitHub issue.
- Journal entry appended.
