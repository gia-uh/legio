# LEG-085 — Runtime: the public face (business submit/status + lifecycle verbs + entry gate)

- **Status:** DRAFT (awaiting maintainer approval)
- **Rasante:** R-8
- **GitHub issue:** #49 (provisional — no `gh` access; the maintainer holds it)
- **Source:** `docs/PLAN.md` (R-8), `docs/AGENT_LIFECYCLE.md` §0/§4/§5/§6/§7/§8/§12
- **Depends on:** beaver (system substrate), `legio.naming` `validate_node_id`
  (LEG-016), LEG-083 (`legio.manager.Manager`), LEG-084 (`legio.registry.Registry`),
  `legio.config` `LifecycleConfig` (LEG-081), `legio.patterns.schema1` `AgentSpec`
  (LEG-010)

## Goal

Implement the **`Runtime`** (`legio.runtime`) — the runtime triangle's third
layer and the **public face** of §0/§6: the only layer with initiative. It owns
the **class entry gate** (§12.5), the business **`submit`/`status`** (mounted on
the Manager's `seed` task, ARCHITECTURE §7), and the **lifecycle verbs** `create_class`,
`recreate_class`, `create_instance`, `destroy_instance`, `enable_class`,
`disable_class`, `destroy_class`, `enable_instance`, `disable_instance`. Every
operation is exactly **action → fact → confirm → record**: the Runtime asks the
`Manager` to perform the real fact, confirms it is a **Manager read** (a bounded
clock wait over the lifecycle budgets (§5.8/§10.2),
then tells the `Registry` to
record it, posteriori.

This slice supersedes the session-79 design per the maintainer's approved
decoupling decision (session 80): the business `submit`/`status` no longer live
in a Runtime-private `business_tasks` scope — the business record **is** a
genuine Manager task (the `seed` task, whose `kwargs` carry the owner/token/
re-keyed payload); the Runtime owns **only** the `gates` scope on beaver; and
the Runtime **does not pump** — the node (the boot's executor, step 4) drives
`manager.run()`, and the tests drive the same loop. The Manager stays blind to
the domain (rule 7): it never inspects what a `seed` record means.

## Scope

- **In scope:** the `Runtime` class (module `legio.runtime`); construction
  binding over the shared db (own `Manager` + `Registry`); the business
  `submit`/`status`; the 9 lifecycle verbs; the read delegations; the **class
  entry gate** (`db.dict("gates")`, written only by the Runtime, §12.5); the
  one-shot bring-up task (fixed task name, in-process callable); the
  `seed` task (fixed task name, in-process callable); the transient
  instance↔task orchestration map (never persisted, §4.8); the drain/now
  destroy resolution (§4.6); the dependency cascade on disable/destroy (§7);
  `recreate_class` from the cached YAML (§4.7/§4.8); the bounded clock waits
  resolved from `LifecycleConfig` (built-in defaults / config, §10.2).
- **Out of scope:** the `Manager` class and its executor (LEG-083, done); the
  `Registry` (LEG-084, done); pools resolution from `PoolsConfig` (LEG-080, step
  4); actual agent materialization — the agent classes themselves, `AgentBase.run`,
  and the bring-up callable's real interior (the boot's job, step 4); the boot
  wiring that gives the **node** its executor and re-targets `api.py`/CLI to it
  (LEG-081/LEG-080); the DAG/routing/delivery (agent concern); the
  `materializer.NodeRuntime` bag (booted node; it will adopt a `Runtime`
  reference at step 4 — relationship documented below).

## Contract & design

### The seam: `legio.runtime.Runtime` vs `materializer.NodeRuntime`

`materializer.NodeRuntime` (`src/legio/materializer.py`) is the **booted-node
bag** (config, connected db, catalog, standing agents, `app`) produced by
`boot_node` (LEG-081). `legio.runtime.Runtime` is the **orchestrating public
face** of §0/§6. They are distinct layers: the Runtime decides and orchestrates;
the boot materializes. Step 4 (LEG-080/LEG-081) wires them — the boot builds a
`Runtime` over the same db and runs its own executor loop driving the
`Runtime.manager.run()`; the Runtime never owns an executor (see below).

### Construction and beaver footprint

```
Runtime(db, *, node_id, manager=None, registry=None, lifecycle=None)
```

- `db` is the shared `AsyncBeaverDB`. `db is None` → `TypeError` (rule 9,
  visible). `node_id` must satisfy `legio.naming.validate_node_id`
  (`<name>@<host>`) — an invalid id raises `InvalidNameError` up front
  (castor-style: loud at construction, never mid-operation).
- The Runtime owns **exactly one beaver scope**: `gates` (a `db.dict("gates")`,
  §12.5.2) keyed by class = `{state: enabled|disabled}`. It reaches the
  **Manager only through its public API** (`register` / `submit_task` /
  `status` / `control_mode` / `pause` / `resume` / `cancel` / `run`) and the
  **Registry through its read/write API** — it never opens the Manager's or the
  Registry's beaver scopes directly. The business submission creates a Manager
  task **through `manager.submit_task`** — the record physically lives under
  the Manager's `tasks` scope, but it was created through the public API, not
  by the Runtime writing another layer's scope. No other scope is added.
- `manager` / `registry` / `lifecycle` are injectable (tests); by default the
  Runtime builds its own `Manager(db, node_id=...)`, `Registry(db)` and a
  default `LifecycleConfig()` over the same db. `LifecycleConfig` supplies the
  bounded-clock budgets (`drain_timeout`, `drain_interval`, §10.2), resolved
  per class via `per_pattern`/`per_kind`/`default`/built-ins.
- In-process (never persisted, rule 13): the Manager's callable registry plus
  the Runtime's own
  `_instance_tasks: dict[tuple[class_name, instance_id], task_id]` (§4.8 — the
  bring-up task id per instance, the orchestration handle).

### Executor ownership — the Runtime never pumps

The Runtime is a **decision point, not an executor**: it does not expose
`run()` or `register()` and it never drives the Manager's polling loop. The
**node owns the executor** — the boot runs `manager.run()` in its own loop, and
these tests drive the same loop (`_start_executor`). The one-shot facts
(bring-up, seed) complete on the next node poll; the Runtime confirms them with
a bounded clock wait over the lifecycle budgets (below), never by spinning a
pass of its own.

### The one-shot bring-up task (the create fact, §5.1/§5.2/§6.1)

- Fixed task name `BRING_UP_TASK = "bring_up"`. The Runtime registers an
  in-process async callable under that name at construction — **one-shot**:

  ```
  async def bring_up(class_name, instance_id, queue) -> instance_id
  ```

  It performs step 2 of §5.1 (the agent is up) and returns the `instance_id`
  — a terminal SUCCESS fact. It is **not a parked generator**: there is no
  gate and no parked run vehicle. A boot that materializes real agents
  **overwrites this callable** at the same seam (the real bring-up's interior is
  step 4).
- `create_instance`/`create_class` (per agent):
  `manager.submit_task(BRING_UP_TASK, class_name, instance_id, queue)` →
  `_await_observable_state(task_id, ok=termnal SUCCESS)` (a bounded clock wait,
  below) → `registry.record_instance`. The one-shot SUCCESS is the confirmed
  fact; the create is recorded posteriori.

### Confirmation — a bounded clock wait over the lifecycle budgets (rules 8/9)

`_lifecycle_budget(class_name)` resolves the class's
`(drain_timeout, drain_interval)` from `LifecycleConfig` (built-in
300.0 s / 0.05 s unless configured). All Runtime waits are **bounded
clock reads with an operator-visible expiry**:

- `_await_observable_state(task_id, ok, *, what, class_name)`: read-poll
  `manager.status(task_id)` every `interval` until `ok(record)` holds or the
  `timeout` budget expires. A FAILED record that the predicate does not accept
  raises **immediately** with the task's error (rule 9 — errors are never
  silent). On expiry → a **visible** `RecoverableError` carrying the last
  observed state (or `missing`).
- `_confirm_control(task_id, mode, *, what, class_name)`: same bound, for the
  cooperative verbs — read `manager.status(task_id)` **and**
  `manager.control_mode(task_id)`; return when the record is alive and the
  control mode equals the requested `mode`. The Runtime reaches the Manager's
  `control` scope **only through this API**; it never opens the Manager's
  scopes itself (`gates` is its only direct scope).
- The waits are bounded-**clock** (the §5.8/§10.2 exception) and
  the budgets are **per-class configuration**, not pass counts: there is no
  `_CONFIRM_PASSES`.

### The class entry gate (§12.5)

- One registry `db.dict("gates")`, keyed by class name = `{state:
  enabled|disabled}`. **Written only by the Runtime** in the lifecycle ops;
  **read** by any depositor (`submit`, and by the flow in later slices). A row
  of `{state: disabled}` blocks a new submit; a **row absent = open** (§12.5.3 —
  destroyed class: queue cleared + gate row removed, every deposit blocked at
  the missing gate, no global oracle).
- Written at: `create_class` (birth state), `enable_class` (enabled),
  `disable_class` + cascade (disabled), `destroy_class` (row removed).
- **Exceptions (nothing in, per §12.5).** The gate governs only *new* entry:
  policy A keeps instances draining a disabled class (§4.1); an error-result
  deposit always proceeds regardless of the gate (§12.5.5 — the closed class
  keeps draining and an error must reach its return path).

### Business submit / status (mounted on the Manager `seed` task, ARCHITECTURE §7)

`Runtime.submit(client_id, route, payload) -> task_id` and
`Runtime.status(task_id, client_id) -> TaskEntry` are the public business
surface, mounted on the **single task substrate** — `Manager.submit_task`:

- **`submit`:**
  1. **Gate check first.** `class = route[0][0]`; read `gates` for the starting
     class; `{state: disabled}` → raise a **visible** `RecoverableError` before
     anything is minted or deposited (nothing enters a non-enabled class,
     §12.5.1). Row absent → open.
  2. `task_id = f"{node_id}:{uuid4()}"`; `result_queue = result_queue_key(
     task_id)`; re-key the client payload under `route[0][1]`
     (`first_input_as`); build the root `FlowToken` (`level_route`,
     `current_index=0`, `end_of_level_queue = result_queue`, `level=1`,
     `launcher_class`, `task_id`, fresh `branch_id`, `root=True`).
  3. `manager.submit_task(SEED_TASK, task_id=task_id, client_id=client_id,
     token=<token dump>, payload=<re-keyed payload>)` — the business record is
     the Manager-created `seed` task; the owner/token live in its `kwargs`
     (LEG-083 accepts extra-official kwargs; the Manager stays blind to their
     meaning). Empty route → `ValueError` (as today).
  4. The `seed` callable (registered in-process at construction): validates the
     token is a **root** token (a non-root token raises a visible
     `RecoverableError`), deposits the root `ExecutionRequestMessage` into
     `db.queue(queue_key(token.launcher_class))`, and returns the validated
     token (the Manager records it as the seed's result).
- **`status`:** the business record is the Manager's task — read
  `manager.status(task_id)`; unknown `task_id` → `KeyError`; `kwargs["client_id"]
  != client_id` → `PermissionError` (owner-scoped); a FAILED seed raises its
  error visibly (`RecoverableError`); the completed result is read from the
  final-result queue via a non-destructive `peek` (ARCHITECTURE §7 step 7), which flips the
  reported state to `completed` with the result payload. Otherwise pending →
  `pending` / running → `running`.
- **Ownership decision (teardown, session 83).** The legacy
  `legio.manager.submit/status` module functions, their module-global db/node
  scaffolding (`connect_manager`/`reset_manager`/`close_manager`/`db`/
  `task_registry`/`agent_queue`/`_node_id`/`_DEFAULT_NODE_ID="local"`) and the
  `"local:"` task-id behavior are **removed**. `api.py` is re-targeted to the
  `Runtime`: `create_app(runtime, clients=None, pattern_catalog=None)`; `submit`
  and `status` delegate to `Runtime.submit`/`Runtime.status`. With the legacy
  `"local"` id gone, task ids honor `Manager`/`Runtime`'s `<node>@<host>` mint
  (`Runtime.submit` produces `<node_id>:<uuid>`), and the node pump
  (`manager.run()`) is the only dispatcher of the seed deposit (§7.1).
- Gate apply rule: the submit checks only the **starting** class (the one the
  flow is seeded on, §12.1) — internal route deposits are a later-slice flow
  concern (§12.5 is the authoritative design; `legio` implements the gate the
  submit uses; the flow's own deposit-time gate check lands with the runtime
  flow work).

### Lifecycle verbs → action → fact → confirm → record (§5/§6.1 table)

Conventions: `name` = class name; `state` reads via `registry.class_state`
(effective, §4.4) and `registry.get_instance`. The section-5 flows are
implemented verbatim; each mutation follows the fixed split — **Runtime decides
→ Manager performs the fact → Runtime confirms by Manager read (bounded) →
Registry records, posteriori**. The translate table of §6.1 is authoritative:

| Runtime verb | Manager fact | Confirmed observable | Registry record |
|---|---|---|---|
| `create_instance` | `submit_task(BRING_UP_TASK, …)` | terminal SUCCESS | `record_instance` |
| `destroy_instance` | `cancel(task_id)` | terminal (`success` no-op on a replayed cancel) | `remove_instance` (+ `set_class_state(disabled)` if last) |
| `disable_instance` | `pause(task_id)` | record alive + control `pause` | `set_instance_state(disabled)` |
| `enable_instance` | `resume(task_id)` | record alive + control `run` | `set_instance_state(enabled)` |
| `create_class (pool N)` | N × bring-up fact | all N terminal SUCCESS | `record_class` + `cache_spec` + N × `record_instance` |
| `destroy_class` | N × `cancel(...)` | all terminal | N × `remove_instance` + `remove_class` |
| `enable_class` | ensure 1 instance + resume all | per-instance alive + control `run` | instances enabled + `set_class_state(enabled)` |
| `disable_class` | nothing (gate only) | gate closed (Runtime state) | `set_class_state(disabled)` + cascade |

**`create_class(spec: AgentSpec, *, spec_yaml: str | None = None, pool: int = 1)`**
(§5.2, precondition: class does not exist — an existing class is a **no-op**
"already exists", idempotence follows the registry):

1. Preconditions parsed from the `AgentSpec`: `kind = spec.kind` (atomic) /
   `None` (composite); `deps = flattened branch references` (composite) / `[]`
   (atomic). A composite carrying a `kind` or an unknown kind → clear error
   before anything is recorded (registry's kind binding, LOUD).
2. **Gate written at birth** — `gates[name] = {state: disabled}` (closed while
   the pool brings up).
3. **Fact + mirror** — `registry.record_class(name, kind, dependencies=deps,
   queue=queue_key(name), state=DISABLED)` + `registry.cache_spec(name,
   spec_yaml)` when the YAML is provided (posteriori to the gate, once per
   class; absent → no cache).
4. Born **enabled** iff `pool > 0` **and** `registry.dependencies_satisfied(name)`
   (§4.2/§4.3). If born enabled: per agent, one at a time — submit bring-up,
   confirm terminal SUCCESS, `record_instance(ENABLED)` (§5.2 step 4); then the
   final facts — `registry.set_class_state(ENABLED)` and `gates[name] = {state:
   enabled}` (entry open). Else born disabled: `pool == 0` → nothing to bring
   up; `pool > 0` with missing/disabled deps → instances brought up and recorded
   **disabled** (ready once deps are satisfied, §4.2/§4.3) — gate stays closed.
5. Dependencies are **not** created in cascade (§7), down is never touched.

**`create_instance(name, count: int = 1) -> list[str]`** (§5.1, precondition:
class exists — verify via a registry read first, `InvalidNameError`/`KeyError`
if not): `born_state` = the class's **effective** state — instances of a
disabled class are born disabled (§5.1 steps 3-4). Per instance: mint
`instance_id = f"{name}-{seq}"` where `seq` = current recorded count for the
class + 1 (monotonic intraboot; reseeded from the catalog on restart) → submit
bring-up, confirm terminal SUCCESS → `record_instance(instance_id,
born_state)`. Returns the minted instance ids. No instance is ever recorded
before its class (no orphans, §4.8).

**`enable_instance(name, instance_id)`** (§5.3, precondition: exists and
disabled): `manager.resume(task_id)` → confirm record alive + control `run` →
`registry.set_instance_state(ENABLED)`. Already enabled → no-op.

**`disable_instance(name, instance_id)`** (§5.5, precondition: exists and
enabled): `manager.pause(task_id)` → confirm record alive + control `pause` →
`registry.set_instance_state(DISABLED)`. Already disabled → no-op.

**`enable_class(name)`** (§5.4, precondition: exists and disabled):
1. If the class has no instances — bring **one** up first (create instance,
   born disabled per its effective state).
2. Resume **all** the class's instances (`manager.resume` each, confirm control
   `run`) → `set_instance_state(ENABLED)` for each.
3. Final facts: `gates[name] = {state: enabled}` + `registry.set_class_state(
   ENABLED)` (§5.4 "after the facts hold"). A conscious enable with missing deps
   stays enabled — the operator accepts the risk (§4.2). Already enabled →
   no-op.

**`disable_class(name)`** (policy A, §5.6, precondition: exists and enabled):
1. **Gate first** — `gates[name] = {state: disabled}` (entry closed; nothing new
   may enter).
2. `registry.set_class_state(DISABLED)`.
3. **Instances are not touched** — they keep draining the pending work
   (§4.1/§5.6 rows 1-2, disable ≠ destroy).
4. **Cascade up, transitively** (§7): for each transitive dependent (registry
   `class_dependents(name, transitive=True)`), apply the gate
   (`gates[dep] = {state: disabled}`) and `registry.set_class_state(DISABLED)`;
   their agents keep draining, never destroyed. Already disabled →
   no-op.

**`destroy_instance(name, instance_id)`** (§5.7, precondition: exists):
1. `manager.cancel(task_id)` (sets control, re-enqueues).
2. Confirm a **terminal** state — the one-shot bring-up fact is already SUCCESS,
   and a cancel reaching a completed record is a terminal no-op; a still-pending
   cancel lands `failed(cancelled)`. Either is the visible terminal fact —
   `_await_observable_state(task_id, ok=terminal)`.
3. `registry.remove_instance(name, instance_id)`.
4. If it was the **last** instance of the class: `registry.set_class_state(
   DISABLED)` (§5.7 row 2 — the class keeps existing, §4.4/§4.5; the effective
   read reports it disabled even across the two records). If the class is
   otherwise already disabled this is still recorded once.
5. A `destroy_instance` whose `_instance_tasks` has no entry for the instance
   (transient map lost on restart) raises a **visible** `RecoverableError` — the
   Runtime cannot know the task id; re-boot re-binds tasks (open item for step
   4).

**`destroy_class(name, *, mode: Literal["drain", "now"] = "drain")`**
(§4.6/§5.8, precondition: exists, always in hot, irreversible):
1. **Resolve the parameter** (§4.6): `drain` — close the gate first
   (`gates[name] = {state: disabled}`, nothing new may enter), then **wait for
   the class queue to empty** (bounded, **human-scale, not a busy loop**):
   poll `db.queue(queue_key(name)).count() == 0` at the lifecycle interval
   (`drain_interval`, default 0.05 s) up to `drain_timeout` (default 300 s) —
   **both from the lifecycle config, not literal constants**. On expiry →
   **visible failure** (`RecoverableError`), the class left in place (gate
   restored to its prior value; the operator decides: fix or escalate to `now`,
   §4.6). `now` — proceed immediately (the operator consciously accepts any
   pending-item loss).
2. **Destroy all instances** (per `destroy_instance`: cancel, confirm terminal,
   `remove_instance`).
3. **Destroy the queue** — clear the class queue (drain what remains; beaver has
   no queue-deletion API, §12.5.3) and **remove the gate row**
   (`gates.pop(name)`) — once the row is gone every future deposit to that class
   is blocked at the missing gate, no oracle needed (§12.5.3).
4. `registry.remove_class(name)` — the **YAML stays cached** (§4.7).
5. **Cascade up, transitively** (§5.8 row 5/§7): each transitive dependent gets
   its gate set `disabled` and is `set_class_state(DISABLED)`; dependents are
   **never destroyed** by the cascade. **Down (dependencies) is never
   touched** — other classes may depend on them.

**`recreate_class(name, *, pool: int = 1)`** (§4.7/§4.8): precondition — the
class does **not** exist (if it exists → `RecoverableError`; a recreate is not
a restart of a live class). Read `registry.get_cached_spec(name)`; a miss (spec
not cached, e.g. node restarted) → **visible** `RecoverableError`. Parse the
cached YAML through the pattern loader (`load_patterns`, `AgentSpec`); the
parsed spec's `name` must equal `name` (else `RecoverableError`). Then
`create_class(spec, spec_yaml=cached, pool=pool)`. Re-creating does **not**
re-enable dependents (§7 — the "why" is not recorded; re-enabling is an
explicit operator decision).

### Read delegations (all to the Registry, §4.8)

`list_classes`, `class_state`, `class_dependencies`, `class_dependents`,
`dependencies_satisfied`, `list_instances`, `get_instance`, `get_cached_spec`,
`class_kind` — thin forwarders with no added logic (the CLI/HTTP read sets,
§4.8).

### Idempotence and error policy

- Registry idempotence is inherited (**create of existing → no-op**; **destroy
  of non-existent → no-op**; **set_* on non-existent → error**; the Runtime
  shields the operator with a "not found / no-op" reply before calling).
- Every Runtime-level denial is **visible** and typed:
  `RecoverableError` for gate-closed submits, drain expiry, confirm expiry,
  unknown cached spec, missing instance→task binding, recreate-of-existing;
  `InvalidNameError`/`KeyError` for an unknown class/instance; `TypeError` for a
  `None` db. Warnings are logged; exceptions formatted never silently swallowed
  (rule 9).
- Gate row shapes are validated on the write (`{state: enabled|disabled}`,
  pydantic boundary) and on the read (a malformed row reads loudly as closed —
  never silently open).

### Logging (rule 11)

The Runtime provisions `logging.getLogger(__name__)` and emits structured
`key=value` events at every observable point: INFO on each verb's
recorded fact (class/instance created, state changed, destroyed), on submit and
gate opens/closes; DEBUG on confirm polls, drain polls; WARNING on no-ops
(already exists / already enabled / not found) and on any denial the Runtime
itself raises; `logger.exception` never used for expected rejections. Logging
ships **with** the implementation.

## Interface

```
legio.runtime.Runtime(db, *, node_id, manager=None, registry=None, lifecycle=None)

  Constants: BRING_UP_TASK = "bring_up"   SEED_TASK = "seed"

  # business surface (mounted on the Manager seed task, owns the entry gate)
  async submit(client_id, route, payload) -> task_id
  async status(task_id, client_id) -> TaskEntry

  # lifecycle verbs (action → confirm → record)
  async create_class(spec: AgentSpec, *, spec_yaml: str | None = None, pool: int = 1)
  async recreate_class(name, *, pool: int = 1)
  async create_instance(name, *, count: int = 1) -> list[str]
  async destroy_instance(name, instance_id)
  async enable_class(name) / disable_class(name)
  async enable_instance(name, instance_id) / disable_instance(name, instance_id)
  async destroy_class(name, *, mode: Literal["drain", "now"] = "drain")
  async create_from_catalog(catalog, *, pools, spec_yamls, pool_override)  # LEG-086

  # read delegations → Registry
  list_classes / class_state / class_dependencies / class_dependents /
  dependencies_satisfied / list_instances / get_instance / get_cached_spec /
  class_kind (read-only yield to class_kind DB)
```

The Runtime has **no `run()`/`register()`**: the exposed `manager` attribute is
the node's handle to drive the executor (and the boot's wiring point, step 4).
All methods async; records persist under the described scopes; nothing but the
persistent scopes is authority (rule 13).

## Acceptance criteria

Adapted from §5/§6.1/§12.5 for this slice:

- The Runtime is the public face and the translator: every mutation is exactly
  **action → (Manager fact) → confirm (Manager read, bounded clock) → record
  (Registry, posteriori)**; the Manager never knows the Registry and neither
  knows the Runtime's decisions.
- Beaver footprint: `tasks`/`pending_tasks`/`control` (Manager — Manager
  records only, including the business `seed` task it created through its own
  `submit_task`), `catalog`/`instances`/`yaml_cache` (Registry), and the
  Runtime's own `gates`. The layers never write each other's scopes (pinning
  tests); a business record appears under `tasks` **only because the Manager
  minted it** — the Runtime reached the Manager through `submit_task`, never by
  opening `tasks`. The instance↔task handles are in-process only — never
  recorded (no 1:1 mapping, §4.8).
- `create_class` ordering (§5.2): gate at birth → `record_class` + `cache_spec`
  (once) → per agent: brought up & confirmed terminal SUCCESS →
  `record_instance`; born enabled iff deps satisfied **and** pool > 0; pool 0 or
  missing deps → born disabled (instances, if any, born disabled). An existing
  class is a no-op.
- §5.4 `enable_class` with no instances brings **one** up first; then resumes
  **all** and records class enabled after the facts hold. §5.6 `disable_class`
  keeps instances untouched (they drain) and cascades disablement up,
  transitively, via gates + mirrored state — never destroying agents. Cascade
  dependents become `created / disabled`.
- §5.7 `destroy_instance` cancels, confirms a **terminal** state (SUCCESS for a
  replayed cancel of the one-shot bring-up, `failed(cancelled)` for a live
  one), and removes the instance; the last instance leaves the class
  `created / disabled` (effective read agrees, §4.4).
- §5.8/§4.6 `destroy_class` closes entry, drains (bounded, human-scale,
  visible-on-expiry, `drain` default) or proceeds (`now`), destroys all
  instances, clears the queue, **removes the gate row**, removes the class; the
  YAML stays cached; dependents cascade-disabled, dependencies untouched.
  `recreate_class` = create driven by the cached YAML (precondition: class
  does not exist).
- Entry gate (§12.5): `submit` into a `{state: disabled}` starting class is
  **blocked with a visible error before anything is minted or deposited**; an
  absent row is open; the row is only ever written by the Runtime.
- Business `submit`/`status` are mounted on the Manager's `seed` task: the
  `task_id` prefixes `<node_id>:<uuid>`; the record is a Manager task whose
  `kwargs` carry `client_id`/`token`/`payload`; the `seed` callable validates
  the root token and deposits the root `ExecutionRequestMessage` into the
  starting class's queue. No `business_tasks` scope exists (pinning tests).
- The Runtime has no `run()`/`register()`; the node drives the executor via
  the exposed `manager` (pinning tests).
- The legacy module functions and `api.py`'s `"local:"` behavior are gone
  (teardown, session 83): `create_app(runtime, ...)` delegates to the Runtime
  and the flow integration stays green on `<node_id>:<uuid>` task ids.
- Domain-free (rule 7): class/instance names in tests are abstract
  (`one-class`, `two-class`, `transform`); no consumer domain enters `legio`.

## Tests

- Contract tests written first (red), implementation second (green). New file
  `tests/test_leg085_runtime.py` on the `beaver_db` fixture
  (`tests/conftest.py`): `Runtime`s construct over the same db as the Manager
  and the agents; a tiny `LifecycleConfig(default=LifecycleParams(
  drain_timeout=0.2, drain_interval=0.05))` bounds the human-scale wait in the
  drain-expiry test.
- The **node pump**: a background `_start_executor(runtime)` loop drives the
  exposed `runtime.manager.run()` exactly as a booted node would — the Runtime
  itself never pumps. Bring-up/seed facts complete on the node's poll; tests
  await them deterministically (`_expect_seed_success`).
- Patterns: the Runtime's one-shot bring-up (default), the seed callable, the
  `_teardown` best-effort destroy with the pump cancelled last. The assert set
  pins: footprints (Manager + Registry + `gates`, no `business_tasks`), the
  seed task landing in the Manager's `tasks`, ordering (record_class before
  record_instance — no orphans), effective states on reads, gate denies
  (task set unchanged), cascade disablement, drain/now, drain expiry
  (visible + class left), recreate from cache, the one-shot bring-up's terminal
  confirm on destroy, and re-homed submit/status (gate-reject + result queue
  equality against `legio.manager` semantics).
- Flow-driving of the business submit (a `ToolAgent` polling the created class
  queue, §12.2) mirrors `tests/test_flow_integration_decoupled.py`; class/route
  names stay domain-free (`transform`).
- Pinning: `assert not hasattr(runtime, "run")` / `"register"` — the Runtime
  never owns an executor. The node-driven test (`test_node_drives_the_executor_
  through_the_manager`) registers a callable on the exposed `manager` and runs
  one `manager.run()` pass.

## Validation case

A synthetic node lifecycle over the `beaver_db` fixture: `create_class`
(enabled and both disabled births), `create_instance`, `disable`/`enable`
(class + instance, with a 2-level dependency chain and a transitive cascade),
`destroy_instance` (terminal-confirm corollary), `destroy_class` in both `drain`
and `now` (plus a bounded drain-expiry), `recreate_class` from the cached YAML,
a gated business `submit` → `status` cycle through the Runtime (seed task
depositing the root message, node pump driving it), and an owner-scoped status
denial. No consumer domain (rule 7).

## Definition of done

- All acceptance criteria met by running checks (pytest + ruff + pyright);
  full suite green, no regressions (submit/status pins use the Runtime's
  `<node_id>:<uuid>` task ids).
- Contract tests first (red), implementation (green).
- Module logging in place (rule 11) at every observable point.
- Maintainer approval recorded; the maintainer closes the GitHub issue.
- Journal entry appended.

## Open items for maintainer

1. **submit re-home split (resolved by teardown, session 83).** The legacy
   `legio.manager` module functions and `api.py`'s `"local:"` behavior are
   removed; `api.py` re-targets to the `Runtime` instance
   (`create_app(runtime, ...)`), unifying the flow-launch mechanics.
2. **Bring-up interior / executor ownership.** The Runtime ships a one-shot
   default bring-up; real agent materialization, a persistent node executor,
   and re-binding `_instance_tasks` after restart belong to the boot (step 4);
   `destroy_instance` of a map-less instance is a visible error here.
3. **`gates` row absence = open** (§12.5.3) — a destroyed class's missing row
   blocks submissions; a created-but-not-yet-gated class is open until
   `create_class` writes its birth row. Ordering chosen so the row always exists
   by the time the pool brings up.
4. **Clock-wait budgets are config, not literals:** §5.8/§10.2's `drain_timeout`/
   `drain_interval` come from `LifecycleConfig` (built-in defaults overridden by
   class config). The confirm waits reuse the same budgets; there is no
   `_CONFIRM_PASSES` and no `drain_interval_ms`.