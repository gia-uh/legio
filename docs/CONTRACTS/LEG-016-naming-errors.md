# LEG-016 — Naming & errors

- **Status:** CLOSED (implementation green, maintainer approved, issue closed)
- **Rasante:** R-1 (contract)
- **GitHub issue:** #10
- **Source:** `docs/PLAN.md` (LEG-016)
- **Depends on:** none

## Goal
Normative glossary of identifiers and the canonical error taxonomy so names are
type-safe across the codebase, logs and APIs.

## Scope
- **In scope:** identifier types (node, agent, tool, pattern, step, task),
  namespacing, error taxonomy, reserved identities.
- **Out of scope:** string interning decisions beyond contracts.

## Contract & design
- **Identifiers** are cleared, immutable strings; each type has a regex and a
  reserved-value set:
  - node: `node_name@host` (interned, immutable)
  - agent: service/domain names (class-based per Schema 2; no `client:` family —
    the destination lives in the token as `end_of_level_queue`, not in an agent id)
  - tool: arbitrary (consumer namespaced)
  - task: `{uuid}`, globally unique; its final result is collected from the
    starting agent's shared queue, addressed by the helper
    `result_queue_key(agent_name)` → `result:<agent>` (the submit-seeded
    `end_of_level_queue` at level 1, LEG-095 Phase 2), into the per-task
    outbox record `outbox:<task_id>` (`outbox_key(task_id)`)
- **Errors** are typed exceptions with a stable `code`, split into recoverable
  (transient) and unrecoverable (fatal authoring/validation) families;
  boundaries (HTTP, CLI, logs) render them consistently.

## Interface
- Identifier validation helpers + error classes as the module contract.

## Acceptance criteria
From `docs/PLAN.md` (LEG-016), verbatim:
- Identifiers typechecked in module signature; invalid names rejected at
  runtime; error codes and recoverability documented.

## Tests
- Contract tests (red first): naming rules, reserved identities, error mapping.

## Validation case
- Unit-level.

## Definition of done
- All acceptance criteria met by running checks.
- Maintainer approval recorded; the maintainer closes the GitHub issue.
- Journal entry appended.