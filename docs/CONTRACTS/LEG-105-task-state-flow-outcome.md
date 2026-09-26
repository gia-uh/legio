# LEG-105 — Task state reflects the flow outcome

- **Status:** APPROVED by maintainer direction on 2026-09-26 (GitHub #54).
- **Rasante:** R-10.x (hardening of the released `v0.1.0`)
- **GitHub issue:** #54 (finding 2 of the #52 audit umbrella)
- **Source:** external audit of `v0.1.0` (`fbd787e`), finding 2
- **Depends on:** LEG-063 (`TaskState.FAILED`), LEG-085, LEG-095 (outbox)

---

## Goal

`Runtime.status()` reports the **flow outcome**, not the seed dispatch. A task
whose step raised is `failed`, never `completed`.

## Problem

`TaskState.FAILED` is assigned in exactly one place
(`src/legio/runtime/__init__.py:1162`) from `record.status == FAILED` on the
**seed** task. The seed only deposits the root token, so it succeeds no matter
what the flow does afterwards. With a broken tool the record is `COMPLETED`
and the outbox carries `{"error": "..."}` while `state` reports `completed` —
a consumer writing `if state == "completed": use(output)` ships a bug.

## Scope

1. Derive `TaskState` from the outbox `ExecutionResultMessage`: an error
   payload maps to `FAILED`; a clean payload maps to `COMPLETED`. The seed
   record keeps being the authority for `PENDING`/`RUNNING` (no result yet).
2. Preserve ownership enforcement and the existing error-in-`output` shape
   (rule 9 — the error string stays visible).
3. Keep polling-only: no new states, no callbacks; `status()` still returns a
   `TaskEntry` for every terminal state (never raises on a failed flow).

**Supersedes** the LEG-063/LEG-064 expectation that an errored flow (e.g. a step
`policy.timeout` expiry) reports `COMPLETED`: with this slice such a flow is
`FAILED` (the error still travels in `output`). Those scenarios now assert
`FAILED`.

## Non-goals

- No change to the seed task or the outbox schema.
- No retry/requeue semantics.

## Contract changes

### `src/legio/runtime/__init__.py` — `Runtime.status()`
When the record is not `FAILED` but the outbox `ExecutionResultMessage`
carries an error, the returned `TaskEntry.state` is `TaskState.FAILED` (with
`output` still carrying the error). The seed record's `PENDING`/`RUNNING`
still drives the non-terminal states.

## Observability (rule 11)

- `runtime status failed task=%s (flow outcome error)` at INFO when the state
  is derived from an error result.

## Validation

Red-first tests: a step that raises yields `status(...).state ==
TaskState.FAILED` while `output` still contains the error string; a clean flow
still yields `COMPLETED`.
