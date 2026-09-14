# LEG-026 — E2E example: `transform` with a fake tool (domain-free)

- **Status:** DRAFT (awaiting maintainer approval)
- **Rasante:** R-2
- **GitHub issue:** #18
- **Source:** `docs/PLAN.md` (LEG-026)
- **Depends on:** LEG-022, LEG-023, LEG-024, LEG-025, LEG-013

## Goal
First real single-node capability: an end-to-end flow where the client submits
to the `transform` agent, which runs a registered fake tool, and the result
reads back via status — proving the R-2 plumbing with a domain-free example.

## Scope
- **In scope:** in-repo example pattern + fake tool + `submit/status` walk.
- **Out of scope:** LLM (not yet in RASANTE), composites (R-4), auth guard
  (LEG-027 ships the same example behind it).

## Contract & design
- In-repo (no consumer material): `transform` agent; a fake, deterministic tool
  registered in the registry; root task result deposited to the starting
  agent's **final-result queue** (`result:<transform>`) at flow close,
  collected by the `RESULT_DRAIN` intake into the task's outbox record
  (LEG-095 Phase 2).
- Runs against real beaver in green.

## Interface
- Consumes LEG-025 REST endpoints as a client would.

## Acceptance criteria
From `docs/PLAN.md` (LEG-026), verbatim:
- Submitting to `transform` runs the fake tool, deposits the result in the
  task's final-result queue and `status` returns it; a same example visible in
  logs.

## Tests
- The example as a green test treating the REST surface as boundary.

## Validation case
- The E2E example itself.

## Definition of done
- All acceptance criteria met by running checks.
- Maintainer approval recorded; the maintainer closes the GitHub issue.
- Journal entry appended.