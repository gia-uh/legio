# LEG-103 — Audit hardening batch (subagent design/coupling audit, sessions 87-88)

- **Status:** DRAFT overall; Slice 1 (tool execution semantics) APPROVED by maintainer direction on 2026-09-16 (session 89); Slice 2 (pending-gather wakeup) APPROVED by maintainer direction on 2026-09-16 (session 93, Option A; beaver events reserved for a future version); Slice 3 (fan-in exclusion) APPROVED by maintainer direction on 2026-09-16 (session 95, per-slot keys).
- **Rasante:** R-10 (hardening)
- **GitHub issue:** to be opened/mirrored by the maintainer
- **Source:** `docs/PLAN.md` (LEG-103); audit evidence in `docs/JOURNALS/2026-09-15.md` (86i), `docs/JOURNALS/2026-09-16.md` (87-88)
- **Depends on:** LEG-013 (Schema 3), LEG-022 (ToolAgent), LEG-040/042 (composite), LEG-015 (federation), LEG-081 (CLI)

## Goal

Fix, one by one and contract-first, the design/coupling findings of the
sessions 87-88 full audit, without changing the domain-free, polling-only,
transport/lifecycle-separated architecture.

## Backlog (audit order)

1. **Slice 1 — tool execution/policy semantics (Major 1).** APPROVED 2026-09-16.
2. **Slice 2 — composite pending-gather wakeup (Major 2).** APPROVED 2026-09-16 (Option A).
3. **Slice 3 — composite fan-in exclusion (Major 3).** APPROVED 2026-09-16 (per-slot keys).
4. **Slice 4 — legacy global `fed` plane (Major 4).** Spec pending.
5. **Slice 5 — CLI federation token ordering + verified minors/notes.** Spec pending.

## Slice 1 contract (APPROVED)

Amends the undecided execution mechanics left open by LEG-013 (“async
execution mechanism … decided at implementation”):

- Tools may be **sync or async callables**; async tools are awaited, never
  wrapped raw. Async generators are rejected loudly (not a valid tool shape).
- Sync tools run **off the event loop** (`asyncio.to_thread`) so one slow call
  cannot stall every pump.
- The declared per-call **`timeout` is enforced** for both shapes via
  `asyncio.wait_for`; expiry surfaces as a visible `TimeoutError` result
  (rule 9), never silent.
- **`retries` stays 0**: any nonzero declared value fails loudly without
  retrying (the engine never retries a step).

## Acceptance criteria (Slice 1)

- An async tool executes and its value (not a coroutine) lands under `output_as`.
- A slow sync tool and a slow async tool each exceed a small policy timeout and
  surface `TimeoutError` visibly.
- A nonzero `retries` declaration fails loudly without any retry.
- An async-generator tool fails loudly.
- Full suite + ruff + pyright green; no other behavior changed.

## Slice 2 contract (APPROVED)

Replaces the unbounded `asyncio.sleep(0.01)` pending-gather re-poll with
Option A (dual concurrent gets rejected: cancelling a beaver `get` can strand
an already-popped item between DELETE and return):

- With a pending fan-out and both inlets dry, the tick suspends on the class
  inbox with a **bounded substrate wait** (`get(block=True,
  timeout=gather_budget)`, default `0.5` s), then re-polls gathering once.
- Inbox work/control wakes the tick **immediately** — control between
  dispatches is preserved and the 86e deadlock fix holds with no legio timer.
- Join liveness is unchanged: no spurious missing-branch failures; the fast
  paths (non-blocking inbox/gather polls first) are untouched.
- `gather_budget` is an explicit constructor seam (must be positive, refused
  loudly otherwise) so tests drive small budgets; the materializer keeps the
  default.

## Acceptance criteria (Slice 2)

- Pending + dry inlets: the tick suspends ~budget on the inbox (no ~0.01 s
  spin-back).
- No sub-0.05 s `asyncio.sleep` fires during an idle pending tick (only the
  substrate's own cadence may appear).
- A pre-deposited gather result is still collected, completing the join.
- New inbox work while pending is fanned out at once, never waiting the budget.
- Non-positive budgets are refused loudly at construction.
- Full suite + ruff + pyright green; no other behavior changed.

## Slice 3 contract (APPROVED)

Replaces the shared-mutable join record with per-slot keys (single-owner and
beaver-lock designs rejected: affinity machinery and lock TTL semantics are
foreign concepts to the flow; the atomic close below needs neither):

- Fan-out writes one slot key per branch (`<task_id>:<branch_id>` →
  `{index, result}`) in a dedicated slots scope, then the continuation record
  carrying only the ordered `expected` branch list (record presence means all
  slot keys exist).
- Each `_fan_in` worker writes **only its own slot key**, then reads the
  expected slots; partial joins return without writing anything shared.
- The close is arbitrated by **one atomic op**: deleting the continuation
  record — the first deleter builds and resumes, a loser finds it gone
  (`KeyError`) and stands down with a debug event.
- Unknown branch / missing slot / missing fan-out stay loud `ValueError`s
  (rule 9 preserved); slot keys are deleted after resume (best-effort
  cleanup); crash semantics unchanged (no replay anywhere in the engine).

## Acceptance criteria (Slice 3)

- Fan-out layout: continuation with ordered `expected`, no embedded slots;
  slot keys carry fan-out index and empty result.
- Two concurrent branch returns join exactly once: one advance, no stranded
  record, no leftover slots.
- Unknown-branch and no-fan-out results stay loud errors.
- Full suite + ruff + pyright green; no other behavior changed.

## Tests

- `tests/test_leg022_toolagent.py`: five new contract tests (red first).
- `tests/test_tools.py`: async/slow/asyncgen fake tools (domain-free fixtures).
- `tests/test_leg103_slice2_wakeup.py`: five new contract tests (red first).
- `tests/test_leg103_slice3_fanin.py`: four new contract tests (red first).

## Validation case

- Existing `transform` fake-tool paths unchanged and green.

## Definition of done (per slice)

- Spec slice approved, red tests, green implementation, full suite + lint +
  typecheck green, journal entry appended, maintainer closes the issue.
