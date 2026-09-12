# LEG-025 — REST surface over the Runtime (LEG-085)

- **Status:** DRAFT (awaiting maintainer approval)
- **Rasante:** R-2
- **GitHub issue:** #17
- **Source:** `docs/PLAN.md` (LEG-025)
- **Depends on:** LEG-014, LEG-024

## Goal
Expose `submit` / `status` over REST (FastAPI) so a node is usable by client
systems, with ownership-aware status.

## Scope
- **In scope:** `POST /submit`, `GET /status/{task_id}`, ownership checks.
- **Out of scope:** auth middleware (LEG-027) — endpoints exist behind the
  later guard; federation endpoints (R-9); the runtime lifecycle HTTP surface
  (`POST /agent/class` takes the pattern as `{spec}` **YAML** data, §4.8 of
  AGENT_LIFECYCLE) — that is the Runtime's public face (LEG-081),
  not this contract.

## Contract & design
- Endpoints delegate to the Runtime (LEG-085); `status` refuses access to a task
  owned by another client (LEG-014/017 semantics).
- HTTP mapping per LEG-016 error taxonomy (4xx/5xx with `code`):
  401 `unauthorized`, 403 `forbidden`/`access_denied`, 404 `unknown_task`,
  409 `class_disabled`/`task_failed`, 422 `unknown_agent`/`invalid_request`.
- Starting-route resolution: when a `pattern_catalog` is provided, the starting
  agent must be served and entry-capable (`main: true`); an unknown, non-`main`
  or invalidated (LEG-070) starting agent is refused with 422 `unknown_agent`
  before anything is minted. Only when no catalog is provided does the agent
  name fall back to a single-agent route (no catalog ⇒ no declared input
  contract to read the real `input_as`).

## Interface
- `POST /submit {client_id, agent, payload}` → `{task_id}`;
  `GET /status/{task_id}?client_id=` → task state + result.

## Acceptance criteria
From `docs/PLAN.md` (LEG-025), verbatim:
- `submit` creates a task and `status` returns it with final result; a
  foreign-client status request is denied.

## Tests
- REST contract tests (respx/httpx), ownership denial case.

## Validation case
- E2E via HTTP in green (LEG-026).

## Definition of done
- All acceptance criteria met by running checks.
- Maintainer approval recorded; the maintainer closes the GitHub issue.
- Journal entry appended.