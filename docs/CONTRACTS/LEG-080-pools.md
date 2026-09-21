# LEG-080 — Pools (pool_size agents per class)

- **Status:** APPROVED by maintainer direction on 2026-09-21 (GitHub #40):
  the implemented scope is the strict concurrency proof
  (`tests/test_pool_consumers_one_queue.py`); lifecycle verbs on `pool_size > 1`
  remain documented control debt.
- **Closed:** 2026-09-21, GitHub #40 closed as implemented (`7a21922`; `make ci`
  green, 640 passed).
- **Rasante:** R-8
- **GitHub issue:** #40
- **Source:** `docs/PLAN.md` (LEG-080)
- **Depends on:** LEG-023

## Goal
Horizontal capacity: `pool_size` agents of a class consuming the same class
queue concurrently, each item consumed by exactly one agent; single-agent
behavior unchanged.

## Scope
- **In scope:** pool_size (a creation parameter of the class, §4.3 of
  AGENT_LIFECYCLE), concurrent consumption.
- **Out of scope:** cross-node pools (R-9 owns federation).

## Contract & design
- A class of `pool_size = N` has N agents, each running its own internal loop on
  the class's queue (`AgentBase.run`, LEG-023); each loop pops the next item, so
  with N loops N items are processed concurrently.
- `pool_size` is a **creation parameter of the class, given with its YAML spec**
  (`create-class <spec.yaml> [--pool N]`, §4.3/§4.8 of AGENT_LIFECYCLE) — the
  spec is data (patterns as YAML, rule 7), the pool size is operator capacity,
  never part of a loop or of the Manager (the agents' loops are their own; the
  pool size is a creation parameter, not a task property).
- `pool_size: 1` is the default single-agent path. `pool_size: 0` creates the
  class without agents (born disabled, §4.3/§4.4).

## Interface
- Class creation parameter (`pool_size`); instantiation at class create
  (§5.2 of AGENT_LIFECYCLE).

## Acceptance criteria
From `docs/PLAN.md` (LEG-080), verbatim:
- n agents on the same queue process n items concurrently; single-agent behavior
  unchanged.

## Tests
- Contract tests (red first): concurrency, n=1 regression.

## Validation case
- Load test with the `distribute_summary` example (slow fake tool).

## Definition of done
- All acceptance criteria met by running checks.
- Maintainer approval recorded; the maintainer closes the GitHub issue.
- Journal entry appended.