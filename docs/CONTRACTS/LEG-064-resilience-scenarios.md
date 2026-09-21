# LEG-064 — Resilience scenario tests (four explicit CI scenarios)

- **Status:** APPROVED by maintainer direction on 2026-09-21 (GitHub #35):
  four explicit resilience scenarios implemented as green CI tests.
- **Closed:** 2026-09-21, GitHub #35 closed as implemented (red-first tests in
  `tests/test_leg064_resilience_scenarios.py`); `make ci` green (654 passed).
- **Rasante:** R-10.x (hardening over current §8 failure and resilience)
- **GitHub issue:** #35
- **Source:** `docs/PLAN.md` (reconceptualized from R-6 remnants)
- **Depends on:** LEG-063

---

## Goal

Four green CI tests exercising the real mounted engine path (headless node boot → submit → status) for the resilience knobs. No unit mocks of internals; each test is an executable scenario.

## The four scenarios

### Scenario 1: Step timeout (`policy.timeout`)
- **Setup**: A tool pattern with `policy: { timeout: 0.05 }` whose implementation `await asyncio.sleep(0.2)`.
- **Expectation**: The step handling hits the timeout, `TimeoutError` is surfaced as an error result. The task completes with `state: completed` and `output: {"error": "TimeoutError: ..."}`. No crash, no silent hang.

### Scenario 2: Provider outage simulation (disabled class at fan-out, tolerant)
- **Setup**: A composite with two branches; the first branch's first class is `disabled` via lifecycle (or gate closed at fan-out). Composite has `fail_fast: false` (default).
- **Expectation**: Fan-out deposits both branches. The disabled branch's slot receives an error `"deposit blocked: branch class 'X' is disabled (gate closed at fan-out...)"`. The other branch executes normally. Fan-in completes (tolerant), composite's `build_output_as` receives both slots (one error, one success). Task completes with composite output containing the errored branch under its `branch_id`.

### Scenario 3: Composite `fail_fast: true`
- **Setup**: A composite with three branches; branch 0's step raises an error (tool that fails). Composite spec has `policy: { fail_fast: true }`.
- **Expectation**: Fan-out deposits branch 0; its error arrives in the gathering queue. The composite detects the error with `fail_fast=true` and **does not deposit branches 1 and 2** (logs `composite fail_fast triggered`). Join proceeds with only branch 0's slot filled (error). Composite output built from the single slot.

### Scenario 4: Composite-level timeout
- **Setup**: A composite with `policy: { timeout: 0.05 }` whose fan-out + gather takes longer (e.g., branches include a slow tool `await asyncio.sleep(0.2)`).
- **Expectation**: The composite's step handling (fan-out + bounded gather wait) exceeds its declared `policy.timeout`. The `TimeoutError` is surfaced by the runner (`_run_guarded`), routed as an error result. Task completes with `state: completed` and `output: {"error": "TimeoutError: ..."}`.

## Test shape

- File: `tests/test_leg064_resilience_scenarios.py`
- Each scenario is one `@pytest.mark.asyncio` test function.
- Uses the headless boot pattern from `test_leg100_consumer_guide.py`:
  ```python
  loaded = load(config)
  booted = await boot_node(loaded)
  stop = asyncio.Event()
  pumps = [
      asyncio.create_task(executor_loop(booted.runtime, stop.is_set))
      for _ in range(executor_pump_count(booted))
  ]
  try:
      await bring_catalog_up(booted)
      task_id = await booted.runtime.submit("client", route, payload)
      # poll status until completed/failed
  finally:
      await _shutdown_pumps(pumps, stop)
      await booted.db.close()
  ```
- Patterns and tools are inline YAML (temp dirs) or minimal fixture files — no consumer domain names.
- Assertions on `TaskEntry.state` (PENDING/RUNNING/COMPLETED/FAILED) and `output` shape.

## Observability assertions (rule 11)
Each test asserts the expected log events appear (via `caplog` or test's own log capture):
- Scenario 1: `agent step error agent=... error=TimeoutError: ...`
- Scenario 2: `composite branch blocked ... gate=closed`, `composite partial`, `composite joined`
- Scenario 3: `composite fail_fast triggered agent=... task=... branch=...`
- Scenario 4: `agent step error agent=... error=TimeoutError: ...`

## CI gate
`make ci` (ruff + format-check + pyright + pytest) must pass with the four new tests adding to the green baseline (≥640 passed).