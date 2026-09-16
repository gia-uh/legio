# LEG-103 — Audit hardening batch (subagent design/coupling audit, sessions 87-88)

- **Status:** DRAFT overall; Slice 1 (tool execution semantics) APPROVED by maintainer direction on 2026-09-16 (session 89).
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
2. **Slice 2 — composite pending-gather wakeup (Major 2).** Spec pending.
3. **Slice 3 — composite fan-in exclusion (Major 3).** Spec pending.
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

## Tests

- `tests/test_leg022_toolagent.py`: five new contract tests (red first).
- `tests/test_tools.py`: async/slow/asyncgen fake tools (domain-free fixtures).

## Validation case

- Existing `transform` fake-tool paths unchanged and green.

## Definition of done (per slice)

- Spec slice approved, red tests, green implementation, full suite + lint +
  typecheck green, journal entry appended, maintainer closes the issue.
