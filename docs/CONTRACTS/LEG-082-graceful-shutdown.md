# LEG-082 — Graceful shutdown + concurrency semaphores

- **Status:** DRAFT (awaiting maintainer approval)
- **Rasante:** R-8
- **GitHub issue:** #37
- **Source:** `docs/PLAN.md` (LEG-082)
- **Depends on:** LEG-013 (per-tool)

## Goal
Operational hygiene: SIGTERM drains in-flight work before exit; per-tool and
per-LLM concurrency semaphores are honored.

## Scope
- **In scope:** graceful drain, per-tool + per-LLM semaphores.
- **Out of scope:** load-balancing (LEG-080).

## Contract & design
- On SIGTERM: stop pulling new items, finish in-flight steps (their result is
  always deposited), then exit.
- Sempahores cap concurrent LLM calls and per-tool executions; a constrained
  tool simply waits rather than failing.

## Interface
- `Semaphore`-based guards; shutdown hook in the node/server lifecycle.

## Acceptance criteria
From `docs/PLAN.md` (LEG-082), verbatim:
- SIGTERM drains in-flight work before exit; per-tool concurrency cap is
  honored under load (test with a slow fake tool).

## Tests
- Contract tests (red first): drain on SIGTERM, per-tool cap.

## Validation case
- Slow-tool load test (LEG-080 lane).

## Definition of done
- All acceptance criteria met by running checks.
- Maintainer approval recorded; the maintainer closes the GitHub issue.
- Journal entry appended.