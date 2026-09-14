# LEG-014 — Mini-manager contract (Schema 2)

- **Status:** CLOSED (implementation green, maintainer approved, issue closed)
- **Rasante:** R-1 (contract)
- **GitHub issue:** #8
- **Source:** `docs/PLAN.md` (LEG-014)
- **Depends on:** ARCHITECTURE §3, LEG-011, LEG-015

## Goal
Define the mini-manager (`submit`/`status`) and how root-task results are
delivered to the client.

## Scope
- **In scope:** `submit`, `status`, root result delivery to the task's
  **final-result queue**, task ownership tagging.
- **Out of scope:** REST surface (LEG-025), security/auth (LEG-027/LEG-017).

## Contract & design
- Submission: `submit(client_id, agent, payload)` → creates a task, builds a
  level-1 root token whose `end_of_level_queue = result_queue_key(agent_name)`
  (the starting agent's shared final-result queue, LEG-095 Phase 2), polls
  step 1 into the starting agent's queue, tags the `tasks` entry with owner
  `client_id`, returns `task_id`.
- Root delivery: when the root task finishes (end-of-sequence **and**
  `level == 1`) the closing agent writes the result to its
  `end_of_level_queue`, i.e. the final-result queue `result:<agent>` — there
  is **no** `results:{task_id}` store and **no** `client:{task_id}` queue. The
  `RESULT_DRAIN` intake collects it into the task's outbox record
  (`outbox:<task_id>`); the client reads it back via `status`.
- Ownership: `status`/results only readable by the owning client (LEG-017).

## Interface
- `submit(client_id, agent, payload) -> task_id`; `status(task_id, client_id)`.

## Acceptance criteria
From `docs/PLAN.md` (LEG-014), verbatim:
- Mini-manager accepts a `submit` request, tags the task with the owner, and
  delivers the root result to the task's final-result queue; the submitted task
  stays visible (pending) until the root result arrives.

## Tests
- Contract tests (red first): submit, root delivery, ownership denial.

## Validation case
- Root flow via the E2E example (LEG-026).

## Definition of done
- All acceptance criteria met by running checks.
- Maintainer approval recorded; the maintainer closes the GitHub issue.
- Journal entry appended.