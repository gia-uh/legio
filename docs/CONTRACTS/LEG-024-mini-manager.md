# LEG-024 — Mini-manager implementation

- **Status:** DRAFT (awaiting maintainer approval)
- **Rasante:** R-2
- **GitHub issue:** #16
- **Source:** `docs/PLAN.md` (LEG-024)
- **Depends on:** LEG-014 (contract), LEG-011

## Goal
Implement the LEG-014 mini-manager: `submit`, `status`, root result delivery to
the task's final-result queue (`result_queue_key`) and client-owned reading via
`status`.

## Scope
- **In scope:** `submit`, `status`, task ownership tagging (`tasks` registry),
  root result on the final-result queue.
- **Out of scope:** REST (LEG-025), auth (LEG-027).

## Contract & design
- Per LEG-014. `status` enforces ownership (client only sees own tasks).
- `submit` stages the task on the `tasks` registry, seeds the level-1 root token
  with `end_of_level_queue = result_queue_key(agent_name)` (the starting
  agent's shared final-result queue, LEG-095 Phase 2), deposits the root step
  into the starting agent's queue and returns `task_id` (LEG-016); the root
  result is written by the terminating agent (at end-of-sequence **and**
  `level == 1`) to that final-result queue (there is no `results:{task_id}`
  store and no per-task `client:{task_id}` queue). The `RESULT_DRAIN` intake
  collects it into the task's outbox record; `status` reads the record back.

## Interface
- `submit(client_id, agent, payload) -> task_id`; `status(task_id, client_id)`.

## Acceptance criteria
From `docs/PLAN.md` (LEG-024), verbatim:
- Contract test from LEG-014 in green against real primitives.

## Tests
- The LEG-014 contract suite (red-first) executed in green on beaver.

## Validation case
- E2E example (LEG-026) submit/status path.

## Definition of done
- All acceptance criteria met by running checks.
- Maintainer approval recorded; the maintainer closes the GitHub issue.
- Journal entry appended.