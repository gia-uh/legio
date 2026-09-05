# LEG-070 — Cascade invalidation on invalid dependencies

- **Status:** APPROVED — implemented TDD 2026-09-05 (maintainer approval recorded
  in journal Session 40)
- **Rasante:** R-7
- **GitHub issue:** #36
- **Source:** `docs/PLAN.md` (LEG-070)
- **Depends on:** LEG-021

## Goal
Pattern engine integrity: disabling or invalidating a (possibly broken)
pattern transitively disables every pattern that depends on it; the catalog
reflects the invalidation.

## Scope
- **In scope:** dependency graph, transitive invalidation, catalog state.
- **Out of scope:** startup gate behavior (LEG-071 owns fail-fast).

## Contract & design
- Catalog is a dependency DAG; invalidating a node marks all descendants
  invalid and removes them from the served catalog.
- Invalidation is visible (a pattern referencing an invalid dependent is
  reported as disabled), not masked.
- The loaded specs are read-only; invalidation is separate, observable runtime
  state (`Catalog.invalid`), so a disabled pattern keeps its spec record and
  can be re-enabled later (the lifecycle `disable` is reversible).

## Interface
- `Catalog.invalidate(pattern)` — marks the pattern and every dependent
  transitively invalid, returns the newly invalidated set (idempotent: a
  re-invalidation is a no-op). An unknown pattern raises `UnrecoverableError`
  (never silent).
- `Catalog.served()`, `Catalog.is_served(name)`, `Catalog.is_invalid(name)`,
  `Catalog.invalid` — the served set = loaded minus invalid; never serves an
  invalid pattern.
- Guards: `resolve_branch` / `resolve_composite_branches` refuse a branch that
  references an invalid pattern; the API's `_resolve_route` refuses to route a
  starting agent the catalog has invalidated ("agent not served by catalog").
- Logging: `patterns invalidated count=<n> chain=<names>` (WARNING, rule 11).

## Acceptance criteria
From `docs/PLAN.md` (LEG-070), verbatim:
- Disabling one broken pattern transitively disables dependents; the catalog
  reflects it; test verifies the full chain.

## Tests
- Contract tests (red first): `tests/test_leg070_cascade_invalidation.py` —
  served/consistent start state, full-chain cascade (5-deep + multi-branch
  consumer), unknown-pattern never silent, idempotent re-invalidation,
  branch-into-invalid is a reported denial, invalidated `main` not routed.

## Validation case
- Stale-pattern injection fixture (a fresh dependency chain loaded then
  invalidated from a leaf; independent patterns stay served).

## Implementation notes
- `legio.patterns.schema1.Catalog`: dependency DAG over composite `branches`
  (reuse by reference); BFS transitive closure of dependents.
- `legio.patterns.loader.resolve_branch`: invalid-reference refusal.
- `legio.api._resolve_route`: disabled starting agent is never routed.

## Definition of done
- All acceptance criteria met by running checks.
- Maintainer approval recorded; the maintainer closes the GitHub issue.
- Journal entry appended.