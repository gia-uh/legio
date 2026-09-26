# LEG-106 — The idle executor does not spin

- **Status:** DRAFT — awaiting maintainer approval.
- **Rasante:** R-10.x (hardening of the released `v0.1.0`)
- **GitHub issue:** #55 (finding 3 of the #52 audit umbrella)
- **Source:** external audit of `v0.1.0` (`fbd787e`), finding 3
- **Depends on:** LEG-083 (`Manager`), LEG-085 (one-pass run), LEG-082 (standing loop)

---

## Goal

An idle `legio server` consumes effectively no CPU. The executor parks on a
real suspension instead of a tight `asyncio.sleep(0)` loop.

## Problem

`executor_loop` (`src/legio/cli.py`) calls `await runtime.manager.run()` and
then `await asyncio.sleep(0)` forever. `Manager.run()` pops with `block=False`
and returns `0` on an empty queue, and `sleep(0)` yields without idling. A
server with zero tasks burns ~100% of one core (measured: 1,506 CPU ticks over
1,500 wall-clock ticks).

## Scope

1. `Manager.run()` reports whether it did work (e.g. the number of dispatched
   items, already `0` on empty). The executor uses that signal: on an empty
   pass it waits on the pending work instead of re-polling immediately.
2. Two acceptable waits (implementer picks the one that fits the polling
   model):
   - a **bounded blocking wait** on the pending-tasks queue (the sanctioned
     agent suspension, `get(block=True, timeout=...)`), woken by a deposit; or
   - a short `asyncio.sleep(backoff)` on an empty pass.
3. Rule 8 is respected: this is an **idle back-off**, not scheduling by
   sleeping. No work is scheduled via a timer; `next_run_at` semantics are
   untouched.

## Non-goals

- No change to the agent standing loop (already correct).
- No push/callback wake-up seam.

## Contract changes

### `src/legio/manager/__init__.py` — `Manager.run()`
Document/return the dispatched count (0 on empty) so the host can distinguish
an empty pass from work. No behaviour change to dispatch itself.

### `src/legio/cli.py` — `executor_loop`
Wait on the pending queue (or bounded sleep) when a pass did no work; resume
immediately when work was dispatched.

## Observability (rule 11)

- DEBUG `executor idle wait` (rate-limited / first occurrence) when the
  executor parks; the existing dispatch logs cover the busy path.

## Validation

A test asserts an idle executor does not run a tight loop: over a bounded
window with no submissions, the number of empty `manager.run()` passes stays
far below the loop-iteration count (instrumented) — or the process's CPU time
stays within a small budget. Must be deterministic in CI (no wall-clock flakes).
