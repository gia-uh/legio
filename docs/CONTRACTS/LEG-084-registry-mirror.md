# LEG-084 — Registry: posterior mirror of the class/instance catalog + YAML cache

- **Status:** DRAFT (awaiting maintainer approval)
- **Rasante:** R-8
- **GitHub issue:** #48 (provisional — no `gh` access; the maintainer holds it)
- **Source:** `docs/PLAN.md` (R-8), `docs/AGENT_LIFECYCLE.md` §0/§4.7/§4.8/§6
- **Depends on:** beaver (system substrate), `legio.naming.validate_agent_id`
  (LEG-016), `legio.patterns.schema1` `AgentKind` (LEG-010)

## Goal

Implement the **`Registry`** (a.k.a. `AgentRegistry`), the posterior mirror of
the node's agent state at runtime (§0/§4.8): it owns (a) the **live catalog** —
the state of classes / instances / dependencies — and (b) the **runtime cache
of YAML specs** (§4.7). It records facts **after** they happen, never before;
it never initiates, never materializes, never runs anything. Step 2 of the
approved 4-step R-8 plan (Manager → **Registry** → Runtime → boot re-audit +
pools wiring). Additive: no existing module changes.

## Scope

- **In scope:** the `Registry` class (module `legio.registry`), its beaver
  scopes (live catalog + YAML cache), the posterior operations of §4.8
  (`record_class`, `cache_spec`, `record_instance`, `set_class_state`,
  `set_instance_state`, `remove_instance`, `remove_class`), the granular read
  queries (`list_classes`, `class_state`, `class_dependencies`,
  `class_dependents`, `dependencies_satisfied`, `list_instances`,
  `get_instance`, `get_cached_spec`), the kind binding, the identity/ordering
  mirror rules, the derived effective-state read (§4.4), and idempotence /
  error policy.
- **Out of scope:** the Runtime (the public face that decides and calls these —
  its own R-8 slice), the Manager's task execution, the class-queue entry gate,
  `gates`/`semaphore`/`outbox` future scopes (ARCHITECTURE §2), the DAG/routing,
  federation (R-9).

## Contract & design

From `docs/AGENT_LIFECYCLE.md` §4.8 (source of truth), implemented verbatim:

### Beaver footprint (Registry-owned scopes) — ARCHITECTURE §2

| Primitive | Scope | Role |
|---|---|---|
| dict | `catalog` | class records (`name` → `ClassRecord`) |
| dict | `instances` | instance records (key `${class_name}:${instance_id}` → `InstanceRecord`) |
| dict | `yaml_cache` | cached YAML (`name` → spec text), upsert |

Registries are beaver dicts addressed by scope name directly (no invented
substrate). Keys/values are persisted as JSON (`model_dump(mode="json")`).

### Records

- `ClassRecord` — `name`, `kind` (`AgentKind \| None`; `None` = composite),
  `state` (`enabled \| disabled`, the stored explicit decision), `queue` (the
  class queue name), `dependencies` (list of class names it depends on).
- `InstanceRecord` — `class_name`, `instance_id`, `state`
  (`enabled \| disabled`).

### Operations (all posteriori — the fact must have occurred first)

| Operation | Records (posteriori) |
|---|---|
| `record_class(name, kind, *, dependencies, queue, state)` | The class entry **after** its queue was created (§5.2) |
| `cache_spec(name, yaml)` | The YAML, **once**, at `record_class` time (§4.7); upsert |
| `record_instance(class_name, instance_id, *, state)` | One entry per agent actually brought up **and running**; the agent's own identity; never a `pool_size` promise |
| `set_class_state(name, enabled\|disabled)` | A class state change that actually took effect |
| `set_instance_state(class_name, instance_id, enabled\|disabled)` | An instance activity change that actually took effect (§5.3/§5.5) |
| `remove_instance(class_name, instance_id)` | An instance whose agent was actually destroyed (§5.7/§5.8) |
| `remove_class(name)` | The class whose spec + queue were actually destroyed; the YAML stays cached (§4.7) |

### Identity and ordering rules (mirror)

- `instance_id` is the **agent's own identity** (the concrete agent of the
  class), **never** a Manager `task_id` — no 1:1 instance ↔ task mapping.
- Fixed ordering within a create: **queue created (Runtime fact) →
  `record_class` + `cache_spec` → per agent: brought up & confirmed running
  (Manager) → `record_instance`** (§5.1/§5.2). `record_instance` never precedes
  `record_class` (no orphan instances).
- `record_instance` on a class that is **not** in the catalog → **error** (rule
  9): an instance never exists without its class.

### Queries (granular, read-only — the Runtime delegates these)

| Query | Returns |
|---|---|
| `list_classes()` | existing class names and their **effective** state (§4.4) |
| `class_state(name)` | the class's **effective** state — existence + activity (`None` if it does not exist) |
| `class_dependencies(name)` | the class's direct dependencies (empty for an unknown class? **No** — error, rule 9) |
| `class_dependents(name, *, transitive=False)` | the class's direct dependents (inverse of the graph), or the **transitive** upward set (the cascade of §7); empty set for an unknown class |
| `dependencies_satisfied(name)` | all direct dependencies exist and are enabled (§4.2/§4.3 read helper) |
| `list_instances(class_name)` | the class's instances and their state |
| `get_instance(class_name, instance_id)` | one instance record (`None` if it does not exist) |
| `get_cached_spec(name)` | the cached YAML for `recreate_class` (`None` if absent) |

**Effective state (§4.4, derived on reads).** `class_state()` / `list_classes()`
report the effective state: `enabled` only if the stored state is `enabled`
**and** the class has at least one recorded instance. Dependencies are **not**
part of the derivation (a conscious enable with missing deps stays enabled).

### Idempotence and error policy

- Recording a class/instance that already exists → **no-op**.
- Removing a non-existent class/instance → **no-op**.
- `set_class_state` / `set_instance_state` / `class_dependencies` on a
  non-existent entry → **error** (rule 9): a `set` on something that does not
  exist would record a false fact; the `Runtime` shields the operator before
  calling.
- Unknown class in `record_instance` → **error**.
- `cache_spec` is an **upsert**.

### Kind binding

A fixed map `{"linguistic", "tool"}` → concrete atomic agent class, plus
`None` ⇒ `type: composite`. An unrecognized kind or a composite carrying a
kind → clear error **before** anything is recorded. `record_class` validates
kind against the enum: a non-enum value fails at the pydantic boundary (loud,
rule 9); a composite is represented by `kind=None`.

### Logging (rule 11)

Every observable lifecycle point emits structured `key=value` events — INFO on
record/set/remove, WARNING on no-ops (idempotent duplicates), ERROR on rule-9
denials (`logger.exception` never used for expected rejections). Every record
event logs the scope key and the recorded state.

## Interface

- `legio.registry.Registry(db)` — a mirror bound to the shared `AsyncBeaverDB`
  (same db as the Manager and the agents, ARCHITECTURE §2). Constructing
  against a `None` db raises `TypeError` (rule 9, visible).
- All methods async; records persist under the scopes above. Queries read
  through; no caching in memory, no in-process state (rule 13 — nothing but the
  persistent scopes is authority).

## Acceptance criteria

From `docs/AGENT_LIFECYCLE.md` §4.8 (verbatim, adapted for this slice):

- The `Registry` is a posterior mirror: it records only facts the `Runtime`
  confirms, never initiates/materializes/runs; it answers granular queries.
- Beaver footprint exactly `catalog` / `instances` / `yaml_cache`.
- `class_state()` / `list_classes()` report the **effective** state (§4.4): a
  stored `enabled` class with zero recorded instances reads as `disabled`.
- Ordering: `record_class` + `cache_spec` never after `record_instance` for the
  same class (no orphan instances); `record_instance` of an unknown class is an
  error.
- Idempotence: duplicate `record_*`/`remove_*` are no-ops; `set_*` on a
  non-existent entry errors.
- `instance_id` is the agent's own identity, never a Manager `task_id`.
- Rebasing proof (mirror rule): no instance is ever recorded before its class
  exists in the catalog.

## Tests

- Contract tests written first (red), implementation second (green).
- Substitutes: temporary beaver file (`beaver_db` fixture in
  `tests/conftest.py`). No consumer material (rule 7) — class/instance names
  are abstract (`one-class`, `two-class`, `first-agent`, `second-agent`).
- Lifecycle-state repetitions use the registry's own `ActivityState` enum
  (`enabled | disabled`).

## Validation case

The Registry's operate→mirror→query cycle over the `beaver_db` fixture: a
synthetic create (record_class + cache_spec), instance bring-up
(record_instance), en/disable state changes, destroy (remove_*), the effective
state derived on reads, and the dependency graph queries
(dependencies/dependents/satisfied). No consumer domain inside `legio` (rule 7).

## Definition of done

- All acceptance criteria met by running checks (pytest + ruff + pyright).
- Contract tests first (red), implementation (green), full suite green, no
  regressions.
- Maintainer approval recorded; the maintainer closes the GitHub issue.
- Journal entry appended.