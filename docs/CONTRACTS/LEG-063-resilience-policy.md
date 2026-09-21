# LEG-063 — Resilience policy: FAILED state + fail_fast

- **Status:** APPROVED by maintainer direction on 2026-09-21 (GitHub #34):
  implemented resilience hardening — FAILED state exposed, fail_fast knob on composites,
  step timeout contract asserted.
- **Closed:** 2026-09-21, GitHub #34 closed as implemented (red-first tests in
  `tests/test_leg063_resilience_policy.py` + `tests/test_leg064_resilience_scenarios.py`);
  `make ci` green (654 passed).
- **Rasante:** R-10.x (hardening over current §8 failure and resilience)
- **GitHub issue:** #34
- **Source:** `docs/PLAN.md` (reconceptualized from R-6 remnants)
- **Depends on:** LEG-085, LEG-095, LEG-040

---

## Goal

Expose the failure state publicly and give composites a knob to switch from tolerant fan-in (current default, §8.3) to fail-fast fan-out, while keeping the idempotency + visibility model (no leases/heartbeats, Session 37).

## Scope

1. **Public `TaskState.FAILED`** in `legio.runtime.TaskState`.  
   - `Runtime.status(task_id, client_id)` returns a `TaskEntry` with `state = TaskState.FAILED` when the seed's Manager record is `FAILED` — **does not raise** `RecoverableError`. This makes the polling API consistent: every terminal state is readable (polling-only, rule 8).

2. **`AgentPolicy.fail_fast: bool = False`** (Schema 1, extra: forbid).  
   - When a composite has `fail_fast: true` and any branch returns an error result, the composite **does not deposit** the remaining branches (they are cancelled before fan-out). The join proceeds with the errored branch(es) already in slots, and the composite's output is built from whatever slots are filled (the composite's `build_output_as` receives the grouped slots as usual).

3. **`policy.timeout` enforcement** is already implemented (AgentBase._run_guarded: `asyncio.wait_for(self._handle(request), timeout)` → `TimeoutError` surfaced as error result). This spec asserts the contract and adds it to the validation scenarios (LEG-064).
   
   **Note on composite timeout**: The composite's `policy.timeout` bounds the `_handle()` call which performs fan-out deposition only. The fan-in (gather) phase occurs in a separate `process_next` cycle and is bounded by `gather_budget`, not the step timeout. A composite-level timeout spanning fan-out through fan-in is a future enhancement.

## Non-goals

- No retry, no re-queue, no at-least-once execution guards.
- No new lifecycle verbs; `destroy` on pool>1 remains documented control debt.
- No change to the composite's `build_output_as` contract — it still receives a mapping `branch_id → branch_payload` (which may contain `{"error": ...}`) and must compose its own output.

## Contract changes

### `src/legio/runtime/__init__.py` — `TaskState` enum
```python
class TaskState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"  # NEW
```

### `src/legio/runtime/__init__.py` — `Runtime.status()`
Current: raises `RecoverableError` on FAILED record.  
New: returns `TaskEntry(state=TaskState.FAILED, output=...)` with the recorded error available (no exception).

### `src/legio/patterns/schema1.py` — `AgentPolicy`
```python
class AgentPolicy(BaseModel):
    timeout: int | float | None = Field(default=None, ...)
    fail_fast: bool = Field(default=False, description="Composite only: on first branch error, cancel remaining branches before fan-out")
    
    @model_validator(mode="after")
    def _validate_fail_fast_composite_only(self) -> AgentPolicy:
        # Fail-fast only meaningful for composites; on atomic it is ignored but accepted.
        return self
```

### `src/legio/agents/composite_agent.py` — fan-out honours `fail_fast`
In `_fan_out`: after each branch deposit, if the branch's slot receives an error result and the spec has `fail_fast=true`, stop depositing remaining branches and proceed to join with the slots already filled.

## Observability (rule 11)
- `logger.info("composite fail_fast triggered agent=%s task=%s branch=%s", ...)` when fail_fast cancels remaining branches.
- Existing `agent step error` / `composite partial` / `composite joined` logs cover the rest.

## Validation
Red-first contract tests in `tests/test_leg063_resilience_policy.py`:
1. `TaskState.FAILED` exposed and readable via `status()`.
2. Composite with `fail_fast=false` (default) → tolerant fan-in (all branches deposited, errors in slots).
3. Composite with `fail_fast=true` → on first branch error, remaining branches not deposited, join with filled slots.
4. Step timeout (`policy.timeout`) on tool/linguistic/composite → error result routed, task COMPLETED.