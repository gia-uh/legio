# LEG-032 — Example: language-summarize (linguistic tool)

- **Status:** APPROVED (maintainer, 2026-08-28) — contract tests written first, implementation to follow
- **Rasante:** R-3
- **GitHub issue:** #22
- **Source:** `docs/PLAN.md` (LEG-032)
- **Depends on:** LEG-030, LEG-031

## Goal
First linguistic domain-free example: a summarizer flow (linguistic step)
feeding a tool step, validating pass-down of structured output into tool calls.

## Scope
- **In scope:** in-repo example; linguistic→tool composition; scoped registry
  wiring.
- **Out of scope:** real LLM guarantees (fake lingo suffices).

## Contract & design
- In-repo example: `summarize` flow = `[linguistic → tool]`. Linguistic
  produces structured output (fake lingo); tool consumes the dot-opened
  record; the final result lands in the starting agent's **final-result queue**
  (`result:<summarize>`) at flow close, collected into the task's outbox
  record (LEG-095 Phase 2).
- Tests assert final-result-queue scoping and structured pass-down.

## Interface
- Consumes the LEG-025 REST surface (through LEG-027 auth).

## Acceptance criteria
From `docs/PLAN.md` (LEG-032), verbatim:
- Submitting to `summarize` returns structured output through the linguistic +
  tool pair, with the tool receiving the linguistic step's structured output.

## Tests
- Example as green test (fake lingo), coverage of pass-down.

## Validation case
- The example itself.

## Definition of done
- All acceptance criteria met by running checks.
- Maintainer approval recorded; the maintainer closes the GitHub issue.
- Journal entry appended.