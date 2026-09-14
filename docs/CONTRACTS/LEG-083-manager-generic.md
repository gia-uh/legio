# LEG-083 — Manager generic task environment (§6.1)

- **Status:** DRAFT (awaiting maintainer approval)
- **Rasante:** R-8
- **GitHub issue:** #47
- **Source:** `docs/PLAN.md` (R-8), `docs/AGENT_LIFECYCLE.md` §6.1
- **Depends on:** LEG-016 (naming), beaver (system substrate)

## Goal

Implement the **generic, domain-free task environment** the docs call the
`Manager` (`legio.manager`, module and class `Manager`, §6.1): the Runtime
triangle's one task engine that manages *any kind of task* (submit, status,
pause, resume, cancel) and executes registered callables. This is the slice the
docs' write-up ships — additive, so the Runtime and the agents are untouched.

## Scope

- **In scope:** the `Manager` class + `TaskRecord` on native beaver scopes
  (`tasks` dict, `pending_tasks` queue, `control` dict), the task-executor
  polling loop, cooperative pause/resume/cancel, in-process callable registry,
  task ids per naming contract `<node_id>:<uuid>`, the addendum AL regression
  (no `next_run_at`, no scheduling queue, no result queue).
- **Out of scope:** the Runtime (public face, business submit, lifecycle verbs —
  a later R-8 slice), the Registry (posterior mirror, another R-8 slice), the
  agent lifecycle mapping (§6.1 table), the class-queue entry gate (Runtime owns
  it), the DAG/routing/delivery (Runtime/agent concern). The business
  `submit`/`status` surface was re-homed under the Runtime (LEG-085) with the
  session 83 teardown and is served to the API through `create_app(runtime,
  ...)`; this issue is strictly additive to the generic engine.

## Contract & design

From `docs/AGENT_LIFECYCLE.md` §6.1 (source of truth), implemented verbatim:

- **Task** = `name` + `task_id` + `args/kwargs` + `status` +
  result/error + timestamps (`enqueued_at`, `started_at`, `finished_at`).
  Status is one of `pending | running | success | failed | cancelling`
  (`cancelling` is the visible half-open state of a cooperative cancel).
- **A task is NOT an agent cycle / agent loop** — that coupling is forbidden.
- Task id is the Manager's own: `<node_id>:<uuid>` (`legio.naming`, LEG-016);
  **never** the catalog's `instance_id`.
- **Beaver footprint (Manager-owned scopes):**

  | Primitive | Scope | Role |
  |---|---|---|
  | dict | `tasks` | task records (`task_id` → `TaskRecord`) |
  | queue | `pending_tasks` | task ids due now (priority 0) |
  | dict | `control` | cooperative control per task: run \| pause \| cancel (TTL) |

  These scopes carry **only generic task records and their facts**. The client's
  business submission also rides these scopes — as the Runtime's `seed` task
  (`SEED_TASK`, LEG-085). The Runtime reaches the Manager **only through its
  public `submit_task`**, passing the domain-wired facts as extra-official
  `kwargs` (`client_id`/`token`/`payload`) and choosing the `task_id`; the
  Manager never inspects what a seed means (rule 7, §6.1) — and no layer writes
  another layer's scope.

- Result/error travel **inside the task record** — consumers poll
  `status(task_id)`. No result queue, no scheduling queue, no `next_run_at`
  (rule 8: nothing sleeps, nothing waits).
- **Surface:** `register(name, callable)` (in-process: `name` → async fn |
  async generator); `async submit_task(name, *args, task_id=None, **kwargs) ->
  task_id` — the `task_id` is minted as `<node_id>:<uuid>` when `None`, or
  validated via `legio.naming.validate_task_id` when the caller (e.g. the
  Runtime, so the business record rides one id) chooses it; a submit that would
  reuse an already-existing `task_id` raises a **visible** `ValueError`
  (rule 9, no silent clobber);
  `async status(task_id) -> TaskRecord | None`;
  `async control_mode(task_id) -> str | None` (the stored cooperative control
  instruction `run|pause|cancel`, or `None` when no explicit instruction — the
  record itself has no `paused` status, so this read is the confirmable
  cooperative fact for the Runtime's §5.5/§5.6 by-reading confirm);
  `async pause(task_id)` / `resume(task_id)` (disable ≠ destroy: non-terminal
  suspension); `async cancel(task_id)` (terminal, cooperative).
- **Callable registry is in-process code, never a registry** (rule 13) — state,
  the authority of what happened, is always a persistent beaver registry. The
  Manager never sees what a callable means (rule 7, domain-free).
- **Task-executor algorithm** (polling loop over the task queue):

  ```
  1. pending_tasks.get(block=False) → dispatch   (IndexError ⇒ nothing due; run() returns)
  2. dispatch(task_id):
     a. plain callable → status := running (+ started_at), then await it;
        async generator → created parked in-process, and status := running
        (+ started_at) is written only after its first yield lands —
        RUNNING means *parked*, never a dispatch transient. A generator
        raising before its first yield (stillborn, e.g. an unmounted
        bring-up) goes pending → failed with no observable running, so a
        waiter can never mistake the transient for a live vehicle (86c).
     b. cancellable? drive the generator, checking control at each yield:
          run    → advance one step
          pause  → yield without advancing
          cancel → cancelling → failed(cancelled)
        not cancellable? await callable(*args, **kwargs)
     c. success + result | failed + error   (never silent, rule 9)
  ```

  - `run()` polls the pending queue **once, non-blocking** (rule 8) and returns
    the number of distinct task ids dispatched; empty queue is a normal return
    (IndexError ⇒ nothing due, no error surfaced).
  - The cancellable path handles async-generator callables registered in this
    process: drive step by step, inspecting `control` before each `yield`.
    `pause` leaves the generator parked in-process (task stays `running`,
    suspends without a terminal state); `resume` releases it; `cancel` closes
    the generator and lands the record in `failed(cancelled)`.
  - The plain path awaits `callable(*args, **kwargs)`; pause has no effect mid
    await (cooperative only). A cancelled-not-started task never invokes its
    callable.
  - **Executor death:** `get()` is destructive/atomic and a task executes once —
    a crashed executor is not retried and runs are not at-least-once. A crashed
    task is surfaced visibly (record stays visibly `running`, verified in tests)
    rather than silently re-run.
  - **Multi-process concurrency:** several task executors (any process) drain
    the same queue; `get()` is destructive/atomic, so a task executes once.
- **Logging** (rule 11): every observable lifecycle point emits structured
  `key=value` events — INFO on submit/deposit/success, DEBUG on dispatch step,
  WARNING on `failed`/`cancelled`/denials, `logger.exception` on crashes.

## Interface

- `legio.manager.Manager(db, *, node_id)` — a concrete task executor bound to a
  shared `AsyncBeaverDB` and minting task ids as `<node_id>:<uuid>`.
  `node_id` must satisfy `legio.naming.validate_node_id` (`<name>@<host>`).
- Surface (all async): `register`, `submit_task`, `status`, `control_mode`,
  `pause`, `resume`, `cancel`, plus `run()` (one polling pass). `status` of an
  unknown `task_id` returns `None` (no raise); `control_mode` of an unknown
  task returns `None` (no raise).
- Task record persisted under the `tasks` scope keyed by `task_id`.

## Acceptance criteria

From `docs/AGENT_LIFECYCLE.md` §6.1 (this slice), verbatim, adapted:

- The Manager manages **any kind of task** (submit, status, pause, resume,
  cancel); it executes registered callables and never knows what a callable
  means.
- Beaver footprint exactly `tasks` / `pending_tasks` / `control`; no result
  queue, no scheduling queue, no `next_run_at`. These scopes carry only generic
  task records and their facts — including the business `seed` task, minted
  through `submit_task` with extra-official kwargs the Manager never inspects
  (rule 7). There is no layer-private business scope.
- `cancelling` is the visible half-open state of a cooperative cancel; pause =
  non-terminal suspension; cancelled = terminal `failed(cancelled)`.
- Task ids are `<node_id>:<uuid>` and never `instance_id`.

## Tests

- Contract tests written first (red), implementation second (green).
- Substitutes: temporary beaver file (`beaver_db` fixture in `tests/conftest.py`),
  registered async fns / async generators as callables.
- Multi-executor test uses two `Manager` instances on the same db.

## Validation case

- The Manager's executor driving registered callables to `success` / `failed` /
  `cancelled` via the `beaver_db` fixture (this repo's test suite). No consumer
  material inside `legio` (rule 7).

## Definition of done

- All acceptance criteria met by running checks (pytest + ruff + pyright).
- Contract tests first (red), implementation (green), full suite green, no
  regressions.
- Maintainer approval recorded; the maintainer closes the GitHub issue.
- Journal entry appended.