# Agent Lifecycle — dynamic loading and unloading of agents

Scope: how agent **classes** and agent **instances** are created, enabled,
disabled and destroyed, in a fully decoupled architecture.

This document is the authoritative design for the lifecycle of agents. It is
derived from the reference project `voice-notes-api` (used as the baseline) and
extends it with **dynamic** loading/unloading, which the reference does not
have. The vocabulary is deliberately fixed: **create / enable / disable /
destroy**, only — no other verbs such as "down"/"up"/"load"/"unload".

> Design note (AGENTS.md rule: decoupling is a hard rule). No layer ever
> decides the DAG nor the routing. The agent that starts a task concretizes and
> passes the DAG. The `Registry` governs only **existence and lifecycle**
> (create/enable/disable/destroy) as a *posterior mirror of facts* — never the
> DAG, never routing, and never the materialization itself (that is the
> `Manager`'s, orchestrated by the `Runtime`; see §0 and §6).

---

## 0. Decoupled roles: Runtime vs Manager vs Registry vs beaver vs lingo

`legio` keeps five independent layers that never know one another:

- **`Runtime`** — the only layer with initiative, the **public face**, and the
  owner of the domain's **business tasks**. It decides operations, exposes them
  (CLI / HTTP / programmatic), receives the client submit and delegates task
  management to the `Manager` (`manager.submit_task`), orchestrates the Registry
  and the Manager, and **records facts after they happen**. It never executes
  agent work itself. Its functions are detailed in §6.
- **`Registry`** (call it `AgentRegistry` elsewhere) — the **memory of the
  node's agent state at runtime**. It owns (a) the **live catalog** — the state
  of classes/instances/dependencies — and (b) the **runtime cache of YAML specs**
  (§4.7). It is a **mirror of facts**: every entry is written **after** (never
  before) the corresponding fact occurred, so the catalog never reports something
  that does not exist. It never materializes or executes anything.
- **`Manager`** (= task manager = mini-manager — one and the same; the
  runtime-triangle's `legio.manager`) — the **generic, domain-agnostic task
  environment** that manages **any kind of task** (submit / status / pause /
  resume / cancel over tasks that are simply `name + args/kwargs + state +
  result/error + timestamps`). It is the **executor of facts**: the Runtime asks
  it for an *action* in task language, and it is what actually executes the fact.
  It is **blind to the domain and to the Registry** — it only knows
  "run this task / stop this task", never that a task is "the agent lifecycle"
  and never what a task's callable is. A task is **not** an agent cycle (that
  coupling is forbidden). Implemented in `legio` itself over beaver; **castor /
  castor-io is not a dependency and nothing from it is used** (see
  `docs/DEPENDENCIES.md`, "Excluded on purpose"). The `Manager` executes the
  facts the `Runtime` orders and does not know the Registry.
- **`beaver`** — the **system's database**: the single substrate shared by
  everything, exposing Python-like data structures (`queue`, `dict`, `channel`,
  `lock`). It is **not** the Manager's substrate alone — the Runtime, the
  Manager, the agents and the Registry all live on the same beaver db
  (agents poll `db.queue`, the Manager keeps its task dict/queues, the outputs
  and the boards all sit on it).
- **`lingo`** — LLM + structured output (the role the reference's `argo` played
  for LLM interaction).

**The regulating principle — registration is a mirror of facts.** Nothing is
registered before it happens. The vocabulary is strict:

- **Action** — what the `Runtime` asks the `Manager` to do, in task language
  (imperative: run/stop/pause/cancel a task). Opening/closing the class entry is
  **not** an Action: the entry gate is the Runtime's own submission-side check
  (§5.6, §6.1).
- **Fact** — the reality that results in the world (a business task the Runtime
  submitted flows through the starting agent and poll routes a message; a task
  runs; a task is paused/cancelled). It is **provoked/administered by the
  `Manager`** (which the Runtime drives), but it is the *reality itself*, not the
  Manager's output.
- **Record** — what the `Registry` stores, **after** the fact occurred
  (posteriori mirror). Never before.
- **Operation** — what the `Runtime` exposes (create_class, enable_class, ...);
  it is exactly *action → fact → record*.

So the `Runtime` is the **translator** between two languages: it asks the
`Manager` for *actions* (in task language) and then tells the `Registry` which
*records* to store (in catalog language). The `Manager` never knows the Registry;
the `Registry` never knows the Manager; only the `Runtime` knows both and
translates.

There is no central scheduler and nothing sleeps: instances (agents) poll their
own queue over beaver autonomously, and scheduling is a field — never callbacks
or sleeps (AGENTS.md rule 8). Nothing here uses Redis or any broker.

---

## 1. Two levels

Every operation lives at one of two levels:

### 1.1 Class (the agent type / pattern)

- The **definition**: the pattern spec (`PatternSpec`, derived from YAML).
- Its **queue**, named after the type (e.g. `summ`).
- Its **kind binding**: the concrete agent type (`tool` / `linguistic` on
  atomics; the unified `composite` runner on `type: composite`, no kind)
  derived from the spec.
- Its **dependencies** (from the spec): a composite references other classes by
  name (its `branches`). Depended-on classes are the class's
  **dependencies**; classes that reference it are its **dependents**.
- Governed by the **Registry** (existence reflected in the live catalog);
  its instances are brought up by the **Manager** executing the fact at the
  **Runtime**'s direction (§0/§6).

### 1.2 Instance (a concrete agent of the class)

- A concrete agent that runs and **polls** the class's queue with its own
  internal loop (LEG-023).
- An instance exists *in the catalog* only because an **agent was actually
  brought up** (the Runtime decided it, the Manager executed the fact at the
  Runtime's request) and the fact was then recorded in the `Registry` (§0,
  registration-is-a-mirror). There is no "instance supervisor" — the `Runtime`
  is what decides and orchestrates.

---

## 2. The four verbs

Only these verbs, applied to both levels:

| Verb | Meaning |
|---|---|
| **create** | bring into existence something that did not exist |
| **enable** | make active / operational (re-open, resume) — reversible |
| **disable** | make inactive / suspended, without removing existence — reversible |
| **destroy** | remove entirely — irreversible |

---

## 3. Two axes of state

For both a class and an instance:

1. **Existence:** `does not exist` → `created` → (destroyed).
2. **Activity** (only meaningful if it exists): `enabled` / `disabled`.

Verbs map to the axes:

- **create:** `does not exist` → `created` (policy decides if born enabled or
  disabled).
- **destroy:** `created` → `does not exist` (irreversible).
- **enable:** `created & disabled` → `created & enabled`.
- **disable:** `created & enabled` → `created & disabled`.

---

## 4. Verb matrix (Class and Instance)

| Verb | **Class** (type) | **Instance** (agent) |
|---|---|---|
| **create** | define the type + its queue | an agent is brought up; it polls the class's queue (requires the class to exist) |
| **enable** | open entry: accepts new items; pending work is processed | the agent resumes / starts polling |
| **disable** | close entry: does not accept new items; **keeps draining** pending work (policy A) | the agent stops polling (pauses); its class and queue are untouched; other agents keep running |
| **destroy** | **armageddon:** removes the class + its queue + **all** its instances | the agent dies (that instance only) |

### 4.1 Policy A — "disable the class"

Disabling a class means:

- The class's **queue stops accepting new items**.
- Its instances **keep processing the pending work** until drained.
- Re-enabling reopens entry. Nothing is lost, nothing is paused.

Disabling a class does **not** pause its instances; they keep draining.

### 4.2 Enable-by-dependencies (creation rule)

A class is enabled at creation **only if all of its dependencies exist and are
enabled**. Otherwise (a dependency is missing or disabled) the class is created
**but disabled**.

- Creating a class does **not** create its dependencies in cascade (see §7
  dependencies). Only the existing ones are evaluated.
- Enabling a class whose dependencies are missing/disabled is a **conscious**
  operator decision, never automatic: it risks a queue that fills without
  anything processing it.
- Instances inherit the state of their class: if the class is born disabled, its
  instances are born disabled too.
- At the **pattern** level a dependency counts as disabled when the loaded
  catalog invalidated it (LEG-070): a composite whose referenced pattern was
  invalidated is itself invalid and never served.

### 4.3 Pool as a creation parameter

Creating a class takes the **pool size (number of instances) as a parameter**
(default `pool_size=1`):

- `.pool_size > 0` and dependencies satisfied → class is enabled, instances
  created (enabled).
- `.pool_size == 0` → class is born **disabled** (consistent with the corollary,
  §4.4), even if dependencies are satisfied. `0` is the explicit way to create a
  class without instances (leaving it to be brought up later).
- A class born disabled for dependency reasons: its instances, if any, are born
  disabled (ready to be enabled once dependencies are satisfied).

"Create class" is separated from "bring up instances": the bootstrap does both
(passing each class's `pool_size`), the dynamic operator may do either.

### 4.4 Corollary — no instances ⇒ class disabled

- **If a class has no instances, the class is automatically disabled** (there is
  nobody to process).
- **Brings up instances does not by itself enable a class**: a class is enabled
  only when its **dependencies are satisfied (§4.2)** and it **has instances
  (§4.3)**. A class disabled for dependency reasons stays disabled even after
  the first instance is brought up (its instances are born disabled too).
- **Option 2 — bringing up instances of a disabled class is allowed.** Instances
  may be created of a disabled class, to **drain / scale** the pending work.
  New instances of a disabled class receive no new work; they only help drain
  what is already queued (consistent with policy A).
- **Destroying all instances ≠ destroying the class.** The class keeps existing
  (spec + queue) but is disabled. Useful for dynamic resource management:
  bring up many instances, let them drain/work, destroy them, the class rests
  disabled; bring up instances again later to re-activate. The definition, queue
  and spec persist — nothing needs to be re-defined.
- **Effective state is derived on reads (safety net).** The stored state is a
  mirror of what the `Runtime` recorded; but `class_state()` / `list_classes()`
  report the **effective state**: `enabled` only if stored `enabled` **and** the
  class has at least one recorded instance. This makes the "no instances ⇒
  disabled" corollary hold atomically on the read side (no crash window between
  `remove_instance` and `set_class_state`, §5.7), while the stored record keeps
  the explicit decision for future re-enabling: bringing up instances does
  **not** auto-enable (the stored state stays `disabled` until the operator
  enables). Dependencies are **not** part of this derivation — an operator's
  conscious enable with missing deps (§4.2) stays enabled.

### 4.5 Destroy here, and what is not destroyed

- **Destroying an instance** only removes that agent. Class, queue, and other
  agents persist. If it was the last instance, the class becomes disabled
  (still exists).
- **Destroying a class** is the armageddon of that type: it removes the class
  (spec), its queue, and **all** its instances, irreversibly. It is an explicit,
  separate, hard operation — distinct from "destroying all instances".

### 4.6 Destroy parameter: `now` / `drain`

Destruction always happens **in hot** (never in the bootstrap) and is always an
armageddon of the class. It takes a parameter:

- **`now`** — everything goes immediately: spec, queue, all instances, including
  any pending items. The operator must be conscious of the loss.
- **`drain`** — the class is destroyed **once nothing is pending** (it waits for
  its queue to drain before removing spec + queue + instances). Preserves no-loss.
  **`drain` is the default.**

**`drain` semantics:** the queue stays in place until it is
empty; the guarantee is that it **does not grow** (the entry gate is closed, so
nothing new enters); if everything works normally it will eventually finish. A
**large timeout** guards the wait as a safety valve (`drain` and the timeout must
be human-scale, not a busy loop — rule 8): on the (unexpected) expiry the
operation **fails visibly** (rule 9) leaving the class untouched, and the operator
decides what to do (fix the class, or escalate to `now`). A `drain` that never
terminates is therefore always an operator-visible problem, never a silent hang.

By default, destroying all instances of a class only disables it (it persists);
the destructive armageddon (removing the class entirely) is the explicit,
separate operation above.

### 4.7 The YAML and the local cache

- **Creating / re-creating a class is always driven by its YAML spec** (the
  spec shape is defined in §4.10).
- The **runtime YAML cache is owned by the `AgentRegistry`** (§0): it keeps the
  spec of every class that has actually been loaded. If a class is destroyed
  and the node has **not** been restarted, it can be re-created from the cached
  YAML (the operator does not have to re-supply it). The cache is lost when the
  node restarts; the bootstrap **rebuilds the initial state and the initial
  cache** from the catalog.
- The cache is a **mirror of facts**: a spec enters the cache only **after** its
  class was actually created (the fact "the spec was read/loaded" happened), and
  it **persists across `destroy_class`** (the class leaves the live catalog but
  its spec stays cached so it can be recreated).
- **Disabling does not touch the YAML** — the spec stays (in cache / catalog).
- **Destroying removes the class** (queue + instances + active spec), **but the
  YAML is kept**, so someone can re-create it later.
- The cache holds **definitions only** — it does **not** record *why* a class
  was disabled (the "reason / by whom"), which is not stored at all.
- **Which operation requires the YAML:** only **create / re-create**. Disable and
  destroy take only the class name (plus the `now`/`drain` parameter for
  destroy); they never use the YAML.

### 4.8 The `AgentRegistry` and who does what

The `Registry` owns the **runtime state of agent classes** — the live
catalog and the runtime YAML cache (§0, §4.7). It is the **posterior mirror**:
entries are written only after the corresponding fact occurred. It does **not**
materialize, run or destroy anything — that is the `Manager`'s job,
orchestrated by the `Runtime`. Its operations fall into two groups:

- **The `Runtime` exposes every lifecycle operation** (it is the public face,
  §6). For each, it (1) asks the `Manager` to perform the real action, and
  (2) only after success asks the `Registry` to **record the fact**.
- **The `Registry` records and answers queries**; it never initiates.

**Registry operations (all posteriori — the fact must have occurred first):**

| Operation | What the `Registry` records (posteriori) |
|---|---|
| `record_class(name, kind, *, dependencies, queue, state)` | The class entry **after** its queue was created (the class's existence fact, §5.2). Bind kind. Unknown kind → error before anything is recorded. A class in the catalog reflects a real, materialized class. |
| `cache_spec(name, yaml)` | The YAML, **once**, at `record_class` time (→ §4.7 cache). Never per-instance. |
| `record_instance(class_name, instance_id, *, state)` | One entry per agent actually brought up **and running** — the agent's own identity, recorded after the fact; never a `pool_size` promise. Count = sum of real facts. |
| `set_class_state(name, enabled\|disabled)` | A class state change that actually took effect. Unknown class → error (rule 9). |
| `set_instance_state(class_name, instance_id, enabled\|disabled)` | An instance activity change that actually took effect (the pause/resume transitions, §5.3/§5.5). Unknown instance → error (rule 9). |
| `remove_instance(class_name, instance_id)` | An instance whose agent was actually destroyed (terminal, §5.7/§5.8). |
| `remove_class(name)` | The class entry of a class whose spec and queue were actually destroyed (armageddon); the YAML stays cached (§4.7). |

**Identity and ordering rules (mirror):**
- `instance_id` is the **agent's own identity** (the concrete agent of the
  class), **never** a Manager `task_id`: an agent is not a task. The task that
  the Manager executes (the fact) has its own `task_id`; the catalog
  records the agent, not the task — there is **no** 1:1 instance ↔ task identity
  mapping, and no duplicated field.
- Within a create, the order is fixed:
  **queue created (Runtime fact) → `record_class` + `cache_spec` → per agent:
  brought up & confirmed running (Manager) → `record_instance`** (§5.1/§5.2).
  A class is never recorded without a queue; an instance is never recorded
  before its agent is running; `record_instance` never precedes
  `record_class` (no orphan instances).
- **Confirmation is a Manager read.** Every mutation waits for the Manager's
  observable state — `status(task_id)` `running` for a create; `status(task_id)`
  on a paused/exited task for enable/disable; a terminal state for destroy —
  before the `record_*`/`set_*`/`remove_*` call. The Runtime translates
  task-language facts into catalog records.

**Queries (granular, read-only — the `Runtime` delegates these):**

| Query | Returns |
|---|---|
| `list_classes()` | existing class names and their **effective** state (§4.4) |
| `class_state(name)` | the class's **effective** state — existence + activity (`None` if it does not exist) |
| `class_dependencies(name)` | the class's direct dependencies |
| `class_dependents(name, *, transitive=False)` | the class's direct dependents (inverse of the graph), or the **transitive** upward set (the cascade of §7) |
| `dependencies_satisfied(name)` | all direct dependencies exist and are enabled — read helper for the create/enable rules (§4.2/§4.3) |
| `list_instances(class_name)` | the class's instances and their state |
| `get_instance(class_name, instance_id)` | one instance record (does it exist, and in what state?) |
| `get_cached_spec(name)` | the cached YAML for `recreate_class` |

**Idempotence and error policy:**
- Recording a class/instance that already exists → **no-op**.
- Removing a non-existent class/instance → **no-op**.
- `set_class_state` / `set_instance_state` on a non-existent entry → **error**
  (rule 9): a `set` on something that does not exist would record a false fact;
  the `Runtime` shields the operator with a "not found / no-op" reply before
  calling.
- `cache_spec` is an **upsert**.

**Kind binding:** a fixed map `{"linguistic", "tool"}` → concrete atomic agent
class, plus `type: composite` → the composite agent class. An unrecognized
kind or a composite carrying a `kind` → clear error before anything is
recorded.

#### CLI (public face = the Runtime)

The CLI talks to the **Runtime**, not to the registry directly. Each command is a
Runtime high-level operation that performs the fact (via the `Manager`) and
then records it (in the `Registry`), posteriori (§0).

```text
legio agent create-class <spec.yaml> [--pool N]        # Runtime.create_class
legio agent recreate-class <name> [--pool N]           # Runtime.recreate_class
legio agent enable-class <name>                        # Runtime.enable_class
legio agent disable-class <name>                       # Runtime.disable_class
legio agent destroy-class <name> [--mode drain|now] # Runtime.destroy_class (default: drain)
legio agent create-instance <class> [--count N]        # Runtime.create_instance
legio agent destroy-instance <class> [--count N]       # Runtime.destroy_instance
legio agent enable-instance <class> <id>               # Runtime.enable_instance
legio agent disable-instance <class> <id>              # Runtime.disable_instance

legio agent list-classes                               # Runtime → registry (read)
legio agent class-deps <name>                          # Runtime → registry (read)
legio agent class-dependents <name>                    # Runtime → registry (read)
legio agent class-state <name>                         # Runtime → registry (read)
legio agent list-instances <class>                     # Runtime → registry (read)
```

The CLI is a thin wrapper over the **Runtime** — no new logic. Idempotence follows
the registry (create of existing / destroy of non-existent → "ok", no-op).
`recreate_class` is **create driven by the cached YAML** (`get_cached_spec`):
precondition — the class does **not** exist (it was destroyed earlier, §4.7).
Plain text output by default.

#### HTTP API (public face = the Runtime)

The HTTP surface also exposes the **Runtime**. Read operations delegate to the
`Registry`; mutation operations perform the fact (via `Manager`) and
then record it (Registry), posteriori.

| Method | Path | Body | → Runtime |
|---|---|---|---|
| POST | `/agent/class` | `{spec}`(YAML), `{pool}` | `create_class` |
| POST | `/agent/class/{name}/recreate` | `{pool}` | `recreate_class` |
| POST | `/agent/class/{name}/enable` | — | `enable_class` |
| POST | `/agent/class/{name}/disable` | — | `disable_class` |
| DELETE | `/agent/class/{name}` | `{mode?: drain\|now}` (default: `drain`) | `destroy_class` |
| POST | `/agent/class/{name}/instance` | `{count}` | `create_instance` |
| DELETE | `/agent/class/{name}/instance` | `{count}` | `destroy_instance` |
| POST | `/agent/class/{name}/instance/{id}/enable` | — | `enable_instance` |
| POST | `/agent/class/{name}/instance/{id}/disable` | — | `disable_instance` |
| GET | `/agent/class` | — | `Runtime→registry: list_classes` |
| GET | `/agent/class/{name}/dependencies` | — | `Runtime→registry: class_dependencies` |
| GET | `/agent/class/{name}/dependents` | — | `Runtime→registry: class_dependents` |
| GET | `/agent/class/{name}/state` | — | `Runtime→registry: class_state` |
| GET | `/agent/class/{name}/instance` | — | `Runtime→registry: list_instances` |

Responses are JSON. `201` on a real create/recreate; `200` on enable/disable and
queries. Idempotent like the registry: create of an existing class → `200` with a
note ("already exists", not `409`); destroy of a non-existent class → `200`
("not found, no-op"). Real errors (invalid kind, broken YAML) → `400`/`422`.

Also a thin wrapper — no extra logic.

### 4.10 The pattern schema (S1) — one agent spec, mandatory contracts, terse call

> This section states the S1 pattern schema: the unified agent model
> (`type: atomic | composite`, `kind: tool | linguistic`, `branches` for the
> composite), terse tool `parameters` with explicit `{input_as}.{key}`
> resolution, mandatory symmetric contracts, no magic composition. §4.11
> Schema 1 is the compact normative reference; this section expands it.

A pattern is **YAML data** (rule 7) defining **agents** — the only first-class
notion of this design. There is **one** agent specification shared by every
agent; `type` discriminates `atomic` from `composite`, and for an atomic
`kind` names its interior — what executes (`tool` | `linguistic`). The kinds
differ only in their **interior**, never in their contract or in how they are
wired into a flow. The pattern **declares**; the code resource defines the tool's
signature; Schema 3 (`available_tools`) maps name → implementation + policy.
The shape language inside the schemas supports
string/number/integer/boolean/array/object, `required`, `properties` and
`default`. The reference model's flat read + `output_as` namespacing is
inherited as *read semantics*, but S1 replaces its implicit names with declared
aliases (§4.10.2) and its `inputs` projection with the terse `parameters` call
(§4.10.4). No consumer domain, code name or resource contract appears in a
pattern (rule 7).

#### 4.10.1 The schema (one specification for every agent)

```yaml
type: atomic | composite                      # root discriminator; exactly these two
kind: tool | linguistic                       # interior of ATOMIC only (composites are type: composite)
name: <id>                                    # MANDATORY, every agent
description: <text>                           # optional
main: bool                                    # enables being a starting agent (eligibility, NOT position)

# Entry contract — MANDATORY for every agent
input_as: <alias>                             # names the incoming payload space; anti-convention
input_type: text | json | binary
input_schema:                                 # mandatory (all agents; text has no schema)
  type: object
  required: [<var>]
  properties:
    <var>: {type: string|number|integer|boolean|array|object, default: <literal>}

# Output contract — MANDATORY for every agent
output_as: <alias>                            # names the deposited payload space
output_type: text | json | binary
output_schema:                                # mandatory (all agents; text has no schema)

# Interior — ATOMIC only, by kind
tool: <available_tools-key>                   # kind: tool; the CALL + the code's signature
parameters:                                   # kind: tool; terse call (§4.10.4)
  <arg>: <{input_as}.{key} | literal>         #   explicit {input_as}.{key}: no implicit resolution
prompt: "<template with {var}>"               # kind: linguistic

# Interior — COMPOSITE only (type: composite)
branches:                                     # list of branches; each branch an ordered list of PATTERN NAMES
  - - <pattern-name>                          #   step: a BARE reference to a defined agent — by name only
    - <pattern-name>                          #   a composite-name step = inner ramification (reuse by ref)
  - - <pattern-name>
    - <pattern-name>
```

**Branch-exclusive fields, structurally enforced.** `type: atomic` → `kind:
tool` or `kind: linguistic`; `type: composite` → must carry `branches` and no
`kind`. A tool branch cannot carry `prompt`; a linguistic branch cannot
carry `tool`; atomic kinds are exclusive (a step is either a tool or a prompt —
never both). A composite carries neither `tool` nor `prompt` — only `branches`.
Crossed `type × kind`, a missing `kind` on an atomic, missing `name`, both work
keys on one atomic, `parameters`/`prompt` on a composite, a composite without
`branches`, an atomic with `branches`, a schema on a `text` type, a tool
without `tool`, or a linguistic without `prompt` are rejected at parse.
Discriminated union — nothing is inferred by key presence.

#### 4.10.2 Mandatory symmetric contracts

Every agent — atomic or composite — declares an **entry contract** (what it consumes: `input_as`, `input_type`,
`input_schema`) and an **output contract** (what it produces: `output_as`,
`output_type`, `output_schema`). The full triples are **mandatory for every
agent**, with strict symmetry and no exceptions. `input_type`/
`output_type` ∈ {text, json, binary} decide **where a value lives**: text → the
payload *is* the string; json → values are fields of the declared schema;
binary → the payload is the blob (or its reference). A `text` payload has no
schema. Schemas validate at the boundary (rule 9), and the contracts make
**composability checkable**: a use's `parameters`/`branches` steps must satisfy the
used agent's entry contract, and every agent's output becomes a producer for
its scope's chain.

**Composition is contract compatibility, not exact subset.** The relationship between a step's input and a
(previous) step's output is **not** that one is an *exact subset* of the other.
It is **contract compatibility**: the consuming step's entry contract must be
*satisfiable* by what the producer's output guarantees — the fields it consumes
exist with the promised types. `fetch` yields `{body: string}`; a following step
whose contract demands `{url: string}` cannot consume it (composability check
fails), while one demanding `{body: string}` can.

**`input_as` is the anti-convention rule.** The first segment of every read is
a **declared alias** — a node's `input_as` or a producer's `output_as` — never
a reserved word such as "input". Because it is mandatory and declared, a loader
can resolve and verify every reference; an unresolved alias or path is a **load
error** (rule 9), never a silent convention.

#### 4.10.3 The terse call vocabulary

A tool's `parameters` value is **exactly one** of: an **explicit dotted path**
of the mandatory form `{input_as}.{key}` — the agent's own declared `input_as`
followed by a key of its `input_schema` — or a **literal** (a value fixed in
this pattern/call). There is no `{from:}`, `{value:}` or `{default:}` wrapper —
those verbosity forms are not approved; the default lives in the code's
signature and is **never written**. The resolution is **explicit and verbose**:
the `input_as` is written out in every dotted path, never resolved implicitly
against the input content. The written `input_as` must match this agent's
declared `input_as`, and `key` must exist in its `input_schema`; a mismatch is
a **load error** (rule 9). Literal values need no producer. Failures are load
errors.

#### 4.10.4 Interior and its coherence with the contract

- **Tool.** `tool: <name>` selects a Schema-3 `available_tools` resource;
  `parameters:` is the **terse call** — each parameter of the executed code's
  signature is an explicit `{input_as}.{key}` dotted path (the agent's own
  `input_as` + a key of its `input_schema`, §4.10.3) or a literal;
  never `{from:}`/`{value:}`/`{default:}`; omitted means the code's default.
  The tool's `input_schema` must be a valid subset of the registered signature —
  checked at load against the registry, so the declared contract can never be
  fiction. Static (load) verification is the flow-against-itself; verification
  against the tool's signature is execution-time (Schema 3).
- **Linguistic.** `prompt:` is a template whose `{var}` placeholders **are**
  the entry contract: every template variable must exist in `input_schema` and
  every declared variable must be used or flagged. The output contract is a
  declaration, not a code signature — it must be **enforced at runtime**
  (structured output / validation against `output_schema`), or the contract is
  a promise that nothing checks (rule 9).
- **Composites.** A composite is `type: composite` with `branches` — a list of
  branches, each an ordered list of **steps**. A step is a **bare pattern name**
  (a reference to a defined agent); there is no inline definition and no
  `input_as`/`output_as` written on the step. The `(class, input_as)` pair that
  the route needs is **resolved by the loader** when it builds the DAG of each
  level/branch (Schema 2); the `input_as` under which a step reads in its
  specific use is derived there, never typed in the agent definition. There are
  **no nested `branches`** and **no inline nodes**: to express inner branching,
  a step's name references a `type: composite` agent. There is no `emit:` and no
  "last child renamed": **how a composite combines its children's results into
  its own output is the agent's implementation**, and the declared
  `output_as`/`output_schema` is the contract that implementation must satisfy.

#### 4.10.5 Reuse belongs to every definition, and encapsulation

Any agent — atomic or composite — is defined once, **source-agnostic**, and is
**wired at the usage site**: a composite references it by name as a step, and
the loader resolves each step's `(class, input_as)` when it builds the DAG; the
handoff re-keys the produced value under the consuming step's (resolved)
`input_as`. The same definition used in different composites binds
to different inputs; the interior never changes. The **same agent may appear
more than once in a flow by position** — distinguished by `current_index` —
unless inside its own definition (cycle → infinite recursion, rejected at
catalog load). Reuse is constrained by **contract composability** (§4.10.2):
repeated identical steps only chain if each one's entry contract is satisfiable
by the previous output, so the apparent ``output_as`` collision dissolves. The used
agent's contract is kept: keys are a subset of its declared properties,
`required` covered unless a `default` exists, extras are load errors; the used
agent's `output_as` becomes a producer for later siblings.

Encapsulation: an agent's interior is **opaque** — a consumer sees only its
`output_as`/`output_schema`, never its internal names (those are checked
against the agent's own scope). **`output_as` is unique within its scope**
(collision = load error). Composition **cycles** (an agent transitively
containing itself) are detected at catalog load and rejected.

#### 4.10.6 Composite output (no `emit`)

A composite declares its output contract (`output_as`/`output_type`/
`output_schema`) up front. **How** it produces that output from its children's
results is **the agent's implementation** — there is
no `emit:` map and no "last child renamed" rule. A composite fans out to its
`branches`, gathers their results on its gathering queue, and its
implementation assembles the declared output from them. The contract is the
guarantee the implementation must satisfy; nothing more is specified.

A composite's steps bind from the composite's `input_as` (the loader resolves,
per step, the `(class, input_as)` pair under which it reads, re-keying from the
composite's input or a prior sibling's produced value); any serial dependency
between steps is expressed by order within a
branch, and any parallel split is another branch — there are no nested
`branches`; an inner split is a step whose name references a `type:
composite` agent (no hidden ordering, no races).

#### 4.10.7 `main` = eligibility to be a starting agent

`main: true` enables an agent to be a **starting agent** (a valid `submit`
entry). At submit the flow is seeded on the chosen `main` agent in **level 1**
with `end_of_level_queue` set to the
**final-result queue** (Schema 2). A `main` agent may also appear
in the middle of another DAG (referenced by name as a step): there it executes
its
functionality as part of the enclosing flow and advances, returning to the
client only when the flow started at it. A catalog may declare several `main`
agents. `main` is **eligibility**, never position: it never changes the
contract or the calling mechanism, and it does not decide which agent is used
where.

#### 4.10.8 Checkable composability (redundancy against the silent)

Because every agent declares its entry and output contracts, the loader
verifies the catalog statically: every `branches` step resolves against a
declared pattern, and the loader builds each level/branch route as
`(class, input_as)` with the consuming steps' `input_as` satisfiable by the
producers; every use satisfies the used agent's
entry contract with statically compatible types; `output_as` names are unique
per scope; interiors cohere with their contracts (vocabulary
above); and there are no cycles. A catalog with an invalid spec **fails at
load** (rule 9; LEG-071 dry-run) — nothing silently miswired ever runs. Once
loaded the catalog is a dependency DAG (LEG-070): invalidating a pattern marks
every composite that depends on it invalid and removes it from the served
catalog — never served, never routed, loudly.

---

## 4.11 The three schemas — current reviewed state (S1 pattern · S2 token · S3 tools)

> **Status.** This section records the schemas **as they stand now** — the
> agreed vocabulary, validated by the S4 simulation (no remaining problems),
> and consistent with §4.10 above. English-only, per AGENTS.md.

### Schema 1 — the agent pattern (S1)

One spec for every agent, symmetric mandatory contracts. Unified model:
`type` discriminates `atomic` from `composite`; `kind` names only the interior
of an atomic (`tool` | `linguistic`); a composite is identified by
`type: composite` + `branches` (no `sequence`/`parallel` kinds). No inline
definitions and no nested `branches` — every agent is defined once as a
`pattern` and referenced **by name**; inner branching is expressed by
referencing (by name) a `type: composite` pattern. A step in a composite is a
**bare pattern name** — it carries no `input_as`/`output_as`, because those are
the agent's own contract and the `(class, input_as)` routing pair is resolved
by the loader when it builds the DAG of each level/branch (Schema 2), not
declared in the agent definition. Approved vocabulary (`emit`/`bind`/
`children` are NOT approved):

```yaml
type: atomic | composite        # root discriminator; exactly these two
kind: tool | linguistic         # interior of ATOMIC only (composites are type: composite)
name: <id>                      # MANDATORY, every agent
description: <text>             # optional
main: bool                      # enables being a starting agent (eligibility, NOT position)
# entry contract — MANDATORY for every agent
input_as: <alias>               # names THIS agent's incoming payload space
input_type: text | json | binary
input_schema:                   # mandatory (json: object with required/properties)
# output contract — MANDATORY for every agent
output_as: <alias>              # names THIS agent's deposited payload space (destination)
output_type: text | json | binary
output_schema:                  # mandatory (json: object with required/properties)
# interior — ATOMIC only, by kind
tool: <available_tools-key>     # kind: tool; the CALL and the code's signature
parameters:                     # kind: tool; arg -> {input_as}.{key} | literal (no from/value/default)
prompt: "<template with {var}>" # kind: linguistic
# interior — COMPOSITE only (type: composite)
branches:                       # list of branches; each branch an ordered list of PATTERN NAMES
  - - <pattern-name>            #   step: a BARE reference to a defined agent — by name only
    - <pattern-name>            #   a composite-name step = inner ramification (reuse by ref)
  - - <pattern-name>
    - <pattern-name>
```

- Contract of entry and output are **mandatory for every agent** — atomic and
  composite — the full triples (`as`/`type`/`schema`), strict symmetry, no
  exceptions. Each agent's `input_as`/`output_as` name **its own** payload
  space; they are not routing fields.
- `type: atomic` → `kind: tool` or `kind: linguistic`; `type: composite` → must
  carry `branches` and no `kind`. A tool branch cannot carry `prompt`; a
  linguistic branch cannot carry `tool`; atomic kinds are exclusive; a
  composite carries neither `tool` nor `prompt` — only `branches`. Crossed
  `type × kind`, missing `kind` on an atomic, missing `name`, both work-keys on
  one atomic, `parameters`/`prompt` on a composite, a schema on a `text` type,
  tool without `tool`, linguistic without `prompt` — rejected at parse. A
  composite without `branches` is rejected; an atomic with `branches` is
  rejected.
- **`parameters` resolution is explicit and verbose.** Each dotted path in a
  tool's `parameters` is **mandatorily** of the form `{input_as}.{key}` — the
  agent's own declared `input_as` followed by a key of its `input_schema`
  (e.g. `text: "{article.text}"`). It is never resolved implicitly against the
  input content; the alias is written out. The `input_as` written must match
  this agent's declared `input_as`, and `key` must exist in its `input_schema`.
  A literal needs no producer.
- **Steps reference, they never define.** A step in `branches` is a **bare
  pattern name** — a reference to a defined agent; there is no inline
  definition, no nested `branches`, and **no `input_as`/`output_as` written on
  the step**. To get inner branching in a step, that step's name references a
  `type: composite` agent (reuse by reference, recurse). The `(class, input_as)`
  pair that the route needs is **resolved by the loader** when it builds the DAG
  of each level/branch (Schema 2): the `input_as` under which a step reads in
  its specific use is derived there, never typed in the agent definition.
- **`main` enables being a starting agent.** A catalog may declare several
  `main: true` agents; each is an **eligible starting agent**. At submit the
  flow is seeded on the chosen `main` agent in level 1 with `end_of_level_queue`
  = the final-result queue; the input contract (its `input_as`) is the flow's
  input and its output contract (its `output_as`) is the flow's output (§12.1).
  `main` is eligibility, not position — a `main` agent may also be referenced by
  name as a step mid-DAG.
- **Reuse:** a definition is unique; each use in a composite is a distinct
  node. Same-class agents may appear more than once in a flow (by position),
  unless in their own definition (cycle → infinite recursion, rejected at
  catalog load, §4.10.5).
- **ToolAgent `parameters` is the CALL, terse:** `parameters: {arg:
  {input_as}.{key} | literal}` — no `{from:}`, `{value:}`, `{default:}`; the
  default lives in the code's signature and is never written. The tool names
  its resource via `tool: <name>`; the name must exist in Schema 3's
  `available_tools`.

### Schema 2 — the token/message that travels between class queues

Settled token fields. No `next_queue` — the next step is derived
by position. No results store — the final destination is the final-result queue.

| Field | Role |
|---|---|
| `schema_version` | int, `1000`; mismatch → reject. |
| `level_route` | tuple of `(class, input_as)` — the route of this level (a branch or sub-sequence), resolved by the loader when it builds the DAG of each level/branch. |
| `current_index` | int, 0-based position of the class processing in `level_route` (advance `+1` = next). |
| `end_of_level_queue` | queue at the end of this level's sequence — created by the **submit** (final-result queue) or by a **composite** (its gathering queue). |
| `level` | branch-depth counter: starts at 1; branching +1; leaving a branch −1. End-of-sequence AND `level == 1` ⇒ flow finished. |
| `launcher_class` | class of the agent that started the flow; constant, informational, not control. |
| `task_id` | str, the process's public id. |
| `branch_id` | str, a composite's branch slot identity: **autogenerated** as a `uuid.uuid4()` (the same stdlib mechanism as `task_id` — no parent-derived naming, collision-free by construction) to avoid unexpected name collisions, assigned by the **submit** at flow birth (the root branch) and by each **composite** at fan-out (its child branches); **every message of the flow carries one** (never empty); constant along the branch's sequence, distinct between branches, preserved unchanged through the branch's whole execution, and used by the fan-in as the join slot. |
| `message_type` | enum `execution_request` \| `execution_result`. |
| `payload` | the data (single container for both roles). |

- **Advance (request):** `current_index < len(level_route)-1` → deliver `payload`
  to class `level_route[current_index+1]` (by position, no next_queue field),
  **re-keying the produced value under `level_route[current_index+1].input_as`**
  (§12.1).
- **End-of-level:** `current_index == len(level_route)-1` → deliver to
  `end_of_level_queue`.
- **Flow end:** end-of-sequence AND `level == 1` ⇒ final: deliver to
  `end_of_level_queue` (= the final-result queue set by the submit). Nothing more
  is routed.
- **Submit assigns the root `branch_id`:** at flow birth the **submit** assigns
  the **root** `branch_id` to the starting agent's request, so **every message
  of the flow carries a `branch_id`** (never empty). The root id is the one the
  starting agent's result is delivered under at final end; each composite
  assigns new, independent autogenerated ids to its child branches.
- **Composite (branching):** a composite class receives its request and does not
  advance while its branches run; it fans out giving each branch its `level_route`
  (the loader-resolved `(class, input_as)` route) and `current_index = 0`,
  incrementing `level` (+1), and **autogenerating a fresh, unique `branch_id`**
  for each branch (independent — no parent-derived naming; distinct from its own
  and from every other branch, across all levels, to avoid name collisions;
  constant along the branch's sequence, carried on the message). Branches
  return to the composite's **gathering queue** via their `end_of_level_queue`;
  the returning result's `branch_id` names the join slot (so a multi-step branch,
  whose returned `level_route` expands to its own internal stages, still slots
  under the parent-assigned id). On fan-in completion the composite decrements
  `level` (−1) and resumes its own level (`current_index + 1` → next of its
  level), with `end_of_level_queue` the one its creator supplied.
- **Composite as root:** submit passes its route as level 1 with
  `end_of_level_queue` = final-result queue; branches run at level 2 with
  gathering; after fan-in the composite advances its level-1 route with the
  submit's final-result queue.
- **`root`** lives in the FlowToken (subclassing the message), not in the queue
  message; `end_of_level_queue` = final-result queue is the store-free return
  (there is no "results store"). The agent does not decide where to deposit —
  who creates the flow assigns the class queue; the information lives always in
  the token.

### Schema 3 — the tools declaration

Each tool is an independent, autosufficient resource; it does **not** know which
agents use it (no coupling). The tool identifier is the explicit `available_tools`
key, and that name is the bridge used later in the agent's `tool: <name>`.

```yaml
available_tools:
  <name>:
    implementation: <dotted.path.to.resource>
    policy: { timeout: <seconds per call>, retries: <call retries> }
```

- A tool does **not** declare its output capacity — the consuming agents
  declare the output via `output_as`/`output_schema`.
- Static (load) verification is the flow-against-itself; verification against the
  tool (its signature/parameters) is execution-time (dynamically loaded).

---

## 5. Flows (states explicit at every step)

Conventions: class state = existence + activity; instance state = existence +
activity; "polls/drains" = an enabled agent consuming its class's queue.

**Who does each flow (all of §5):** every mutation flow follows the same split —
the **Runtime** (public face, the authority that creates/destroys/enables/
disables agents) exposes and orchestrates; it asks the **Manager** to
perform the real fact (bring up/destroy an agent, run/stop its task); and
**only after** that fact is confirmed it asks the **Registry** to **record
it** (§0, registration-is-a-mirror). **Confirmation is a Manager read** (§4.8) —
every mutation waits for the observable state (`status(task_id)` `running` on
create; a paused/exited `status` on enable/disable; a terminal state on destroy)
before the `record_*`/`set_*`/`remove_*` call. The class **entry gate** is not a
Manager fact — it is the Runtime's own submission-side check against the
catalog (§5.6, §6.1). The state tables below describe the resulting transitions;
they happen *after* the fact, never before. Reads ("verify the class exists")
always go to the `Registry`.
There is **no** separate "instance supervisor" — bringing up agents is the
Runtime's orchestration over the Manager.

### 5.1 Create instance
Precondition: the class exists. Bringing up an instance does **not** by itself
enable the class (enablement comes from dependencies satisfied + having
instances, §4.2/§4.4); instances of a disabled class are born disabled.

| # | Action | Class | Instance |
|---|---|---|---|
| 1 | Verify the class exists (read) | created (enabled or disabled) | — |
| 2 | Bring up the agent: its own loop starts running on the class's queue; wait until `running` (Manager read) | unchanged | agent running |
| 3 | Record the instance with the class's **effective** state — born `disabled` if the class is disabled (§4.2/§4.4); initial control `run` (enabled) or `pause` (disabled) | unchanged | `created / disabled` or `created / enabled` |
| 4 | (corollary) having instances is a precondition for the class being enabled, but does not enable it on its own | unchanged | — |

How: **Runtime** → **Manager** submits the task that brings the agent up and
confirms `status(task_id) == running` (Manager read) → on success **Runtime** →
**Registry** `record_instance(class, instance_id, born_state)` (posteriori). If
the agent never reaches `running` — nothing is recorded (the mirror never
reports an agent that was not actually brought up).

**No instance supervisor** — the Runtime orchestrates this (§5 convention).

### 5.2 Create class
Precondition: the class does not exist. Creation takes `pool_size` as a
parameter and evaluates dependencies; it does **not** create dependencies in
cascade.

| # | Action | Class | Instances / Queue |
|---|---|---|---|
| 1 | Create the type's queue (Runtime fact) | `created / disabled` | queue created (accepts nothing while disabled) |
| 2 | `record_class(name, kind, deps, queue, state)` + `cache_spec(name, yaml)` (posteriori, once) | `created / disabled` | — |
| 3 | Evaluate dependencies (all exist and are enabled?) and `pool_size` | (see below) | — |
| 4 | If all dependencies satisfied and `pool_size > 0`: per agent — bring it up, confirm `running`, `record_instance` (enabled) | `created / enabled` | instances created / enabled |
| 5 | Else (a dependency missing/disabled, or `pool_size == 0`): born disabled; instances, if any, born disabled | `created / disabled` | instances, if created by pool, `created / disabled` |

How: **Runtime** creates the type's queue (fact) → `record_class` + `cache_spec`
(posteriori to the queue, **once per class**) → for each agent of the pool, asks
the **Manager** to submit the bring-up fact (one at a time) and confirms
`running` → on each success **Runtime** → **Registry**
`record_instance` (posteriori). The catalog count is the **sum of real facts**,
never a `pool_size` promise.

In a booted node the bring-up is a **parked async generator** (LEG-087) that
spawns the instance's own `standing_loop` and awaits it, so `running` is not a
transient state: the record reads `running` for the agent's whole life, and the
moment the agent honors a `terminate_with_drain` and exits its own loop the
record reaches `success` at that same structural moment — never a timer or a
poll (rule 8). The bring-up itself consumes no queue work; the agent suspends in
its own class queue.

### 5.3 Enable instance
Precondition: the instance exists and is disabled.

| # | Action | Class | Instance |
|---|---|---|---|
| 1 | Release the agent's loop (`enable` control honored) | unchanged | `created / enabled` |

How: **Runtime** (as the only minting authority) deposits a signed
`ControlMessage(action=enable)` at control priority on the instance's class queue
(the `ENABLE_INSTANCE` fact, LEG-087, with the mint registered in its pending
ledger) → converges on the **state report**: the agent deposits one report per
honored control (`state=ready`, LEG-095) and the Runtime's report intake applies
it to the **Registry** once correlated against the pending mint (no optimistic
write) → the confirm is a bounded Registry read of the target state (§5.8) →
the instance row reads `enabled`. A no-op when the instance is already enabled.

### 5.4 Enable class
Precondition: the class exists and is disabled. Enabling is a **conscious**
operator action. If the class's dependencies are not satisfied, the operator
accepts the risk of a queue filling without anything processing it (§4.2).

| # | Action | Class | Instances |
|---|---|---|---|
| 1 | Check the class has at least one instance; if none, bring one up | — | created / disabled (then enabled) |
| 2 | Mark the class enabled (queue accepts new items again) | `created / enabled` | — |
| 3 | Order **all** the class's instances enabled (`enable` control for each) | unchanged | enabled, draining |

Rule (§4.2/§4.3, symmetric): `enable_class` orders **all** the class's instances
enabled. The registry does not record *why* an instance is disabled (§7), so an
explicit `disable_instance` of a single agent is a one-shot pause that the next
`enable_class` clears.

How: **Runtime** (conscious decision) → ensures an agent exists (via the
bring-up, confirmed `running`, recorded **posteriori** in **Registry**) → orders
**all** instances enabled (a signed `enable` control deposit per instance, each
converging on its state report — §5.3) → **Registry** `set_instance_state(enable)`
applied by the report intake for each + `set_class_state(enabled)` after the
facts hold.

### 5.5 Disable instance
Precondition: the instance exists and is enabled.

| # | Action | Class | Instance |
|---|---|---|---|
| 1 | Order the agent disabled (`disable` control honored; its loop parks locally) | unchanged | `created / disabled` |

How: **Runtime** (the only minting authority) deposits a signed
`ControlMessage(action=disable)` at control priority on the instance's class
queue (the `DISABLE_INSTANCE` fact, LEG-087, ledgered) → converges on the state
report (`state=parked`; the intake applies it once correlated with the pending
mint) → the confirm is a bounded Registry read of `disabled` (§5.8) — no
optimistic write. The agent's own loop
parks — inbox work is held in-memory, losslessly released on `enable` — and stays
alive so `enable` can reach it. Unlike disable-class, here **one specific agent
is stopped**. A no-op when the instance is already disabled.

### 5.6 Disable class (policy A)
Precondition: the class exists and is enabled.

| # | Action | Class | Instances |
|---|---|---|---|
| 1 | Mark the class disabled (queue stops accepting new items) | `created / disabled` | — |
| 2 | Instances keep draining the pending work | unchanged | enabled, draining (not paused) |
| 3 | Cascade-disable dependents (those that reference it) | dependents become `created / disabled` | — |

How (policy A): **Runtime** decides `disable_class`: from then on, its submit
path **rejects new items** against the catalog (the entry gate, §6.1) →
**Registry** `set_class_state(disabled)` records the fact → the agents are
**not touched** — they keep draining the pending work → for each dependent
(transitive), the Runtime gate applies and **Registry** marks it disabled,
agents still draining. **disable ≠ destroy**: disable closes entry, never kills
agents.

### 5.7 Destroy instance
Precondition: the instance exists.

| # | Action | Class | Instance |
|---|---|---|---|
| 1 | Order the agent to terminate (`terminate_with_drain` honored: drain then exit) | unchanged | `does not exist` |
| 2 | (corollary) if it was the last instance, the class becomes disabled | `created / disabled` | — |

How: **Runtime** (the only minting authority) deposits a signed
`ControlMessage(action=terminate_with_drain)` at control priority on the
instance's class queue (the `DESTROY_INSTANCE` fact, LEG-087) → confirms the
**bring-up** record reaches `success` — the structural terminal of the agent's
exit (the message ends the agent's own loop; the record's terminal is its
consequence; **no `manager.cancel` anywhere on this path**) → on success
**Runtime** → **Registry** `remove_instance` (posteriori); if it was the last
instance, `set_class_state(disabled)` — and reads report it via the **effective
state** (§4.4) even across the two records. A destroy of the **last instance
after a reboot** (no in-process vehicle) is a pure Registry fact removal (§4.8);
a bring-up that FAILED for any reason is not a structural exit — destroy refuses
visibly (rule 9).

### 5.8 Destroy class (armageddon)
Precondition: the class exists. Always in hot. Parameter `now` or `drain`
(**default: `drain`**, §4.6).

| # | Action | Class | Instances |
|---|---|---|---|
| 1 | Resolve parameter (`drain`: wait until queue empty, §4.6; `now`: proceed) | unchanged | unchanged |
| 2 | Destroy all its instances | unchanged | all `does not exist` |
| 3 | Destroy the queue | unchanged | — |
| 4 | Remove the spec from the catalog | `does not exist` | — |
| 5 | Cascade-disable dependents (they reference a destroyed class) | dependents become `created / disabled` | — |

Notes:
- **Down (dependencies) is not touched**: the classes this one depends on are
  never destroyed or disabled here, because others may depend on them.
- **Up (dependents)**: classes that reference the destroyed class are disabled
  (cascade, §7) — they remain existing but disabled, never destroyed by the
  cascade.

How: **Runtime** resolves `mode` (`drain` waits until queue empty; `now`
proceeds) → for each instance: deposit a signed `terminate_with_drain` control
and confirm its bring-up record reaches `success` (the structural terminal, §5.7)
→ **Registry** `remove_instance` (posteriori) → the Runtime removes the queue and
its entry gate and the **Registry** `remove_class`
(the YAML stays in the Registry's cache) → cascade-disable dependents (Runtime
gate + Registry `set_class_state(disabled)`; their agents keep draining).
**Down (dependencies) is never touched.** Irreversible.

---

## 6. Responsibilities (who owns what)

The lifecycle splits across the three layers described in §0:

- **`Runtime` (only layer with initiative, public face)** — decides every
  lifecycle operation, exposes it (CLI / HTTP / programmatic), and orchestrates
  the other two. It knows the domain's **business tasks** and receives the client
  submit; for each mutation it asks the `Manager` to submit/manage the task that
  performs the real fact (lifecycle included) and, **only after success**, asks
  the `Registry` to record it (registration-is-a-mirror). It validates reads
  against the `Registry`. Functions: `create_class`, `recreate_class`,
  `create_instance`, `destroy_instance`, `enable_class`, `disable_class`,
  `destroy_class`, `enable_instance`, `disable_instance`, the business
  `submit`/`status`, and the read queries.
- **`Registry` (posterior mirror, owner of the YAML cache)** — records the
  facts the `Runtime` confirms (classes, instances, state changes, specs in the
  cache) and answers the granular queries. It **never initiates, never
  materializes, never runs** anything. Governs only existence and lifecycle —
  **never the DAG or routing**.
- **`Manager` (the generic task environment, executor of facts, blind to the
  domain)** — manages **any kind of task** (submit, status, pause, resume,
  cancel). The Runtime drives it for lifecycle facts and for business tasks; it
  does **not** know that a task is "the agent lifecycle", it never knows the
  DAG, and it is not the submit's public face — the submit arrives at the
  Runtime, which delegates to the `Manager` (§6.1).

The `Manager` holds the task reality (a task runs through it); the `Registry` is
its posterior mirror; the `Runtime` is the decision point between them. An
**agent is not a task**: the catalog records the agent by its own identity; the
Manager task that is the fact keeps a separate `task_id` (§4.8).

**Invariant — no orphaned jobs.** A business task is
always traceable in the Manager's `tasks` — the business record **is** the
Manager's `seed` task (ARCHITECTURE §7), never a
separate layer-private record:
it either reaches a
terminal state or stays visibly pending. The only deliberate-loss path is
`destroy_class --mode now`, which is explicit and operator-chosen (§4.6). There
must never be a job that exists nowhere and is seen by no one. **If an orphan
appears, that is a design error to fix — never a runtime condition to paper
over.**

### 6.1 The `Manager` — generic task environment (design)

The `Manager` (= task manager = mini-manager; the runtime triangle's one task
engine, `legio.manager`) is the reduced, domain-free task environment on beaver,
implemented in legio itself — **castor / castor-io is not a dependency and
nothing from it is used** (`docs/DEPENDENCIES.md`, "Excluded on purpose"). It
knows only **tasks** — a task is a `name` + a
`task_id` + `args/kwargs` + a state + result/error + timestamps. It never knows
agents, classes, agent queues, patterns, the DAG, routing or the `Registry`.

**What a task is (domain-free, decoupled).** A task is a plain unit of work for
the Manager to manage: identified by `task_id`; the record holds `name`,
`args`, `kwargs`, `status`, timestamps (`enqueued_at`, `started_at`,
`finished_at`), `result`, `error`. Status is one of `pending | running |
success | failed | cancelling` (`cancelling` is the visible half-open state of
a cooperative cancel). **A task is NOT an agent cycle / an agent loop** — that
coupling is forbidden. The Manager executes callables; it never sees what a
callable means. The agent's own polling loop (`AgentBase.run`, LEG-023) is
**not** a Manager task: it is the agent's own life, governed by the Runtime's
lifecycle facts (§5, §12.2) — the Manager is not "the thing that runs agent
loops".

The **client submit lives at the Runtime** (the public face knows the domain's
business tasks): the Runtime checks the entry gate, mints the business
`task_id`, builds the root `FlowToken` and — exactly like its lifecycle facts —
calls `Manager.submit_task(SEED_TASK, task_id=..., client_id=..., token=...,
payload=...)` (LEG-083, extra-official kwargs). The business record **is** the
Manager's `seed` task; the in-process `seed` callable (registered by the Runtime
at construction) validates the root token and deposits the root message into
the first class's queue. The Manager is blind to what a `seed` means — it
manages it like any task, and it is reached **only through its public API**
(rule 7; ARCHITECTURE §7). There is no layer-private business scope (no
`business_tasks`): the Runtime's only beaver scope is `gates`. Task id is the
Manager's own
(`<node_id>:<uuid>`, `legio.naming`, LEG-016); it is **not** the catalog's
`instance_id`.

**Beaver footprint (Manager-owned scopes):**

| Primitive | Scope | Role |
|---|---|---|
| dict | `tasks` | task records (`task_id` → `TaskRecord`) |
| queue | `pending_tasks` | task ids due now (priority 0) |
| dict | `control` | cooperative control per task: `run \| pause \| cancel` (TTL) |

The Manager-owned scopes carry **only generic task records** (and the task ids /
control facts that belong to them) — including the business `seed` task, created
**through** `submit_task`, never by another layer opening `tasks`. The Runtime
owns exactly two scopes: the `gates` scope (§12.5) and the **node control
intake `node_ops`** (LEG-088 — the `origin: operator` source, deliberately
distinct from the Manager's `control` task-control scope); layers never write
one another's scopes.

**The operator surface (LEG-088/LEG-081).** Operator lifecycle orders
(CLI/API/peer) never reach the Manager or an agent queue directly — they are
**deposited as intents** on the Runtime's own `node_ops` queue (typed payloads:
`{verb, class, instance}`, validated by the Runtime: known verb, live class,
live instance when the verb addresses one). Deposition **only** queues the
intent and schedules a drain — it mints nothing and never touches an agent
queue. The intake is drained via the `NODE_OP` Manager fact: each dispatch pops
one intent, relays it through the Runtime's decision logic to the corresponding
lifecycle verb (which mints the signed `ControlMessage`, confirms, and records
the Registry posteriori). It is a drain loop whose scheduling **field** is the
presence of work on the intake queue — a data read, never a sleep (rule 8);
a `NODE_OP` drain occupies its executor while it awaits the lifecycle confirm,
so the node runs 2+ executors (the §6.1 executor-occupancy topology).

Result/error travel **inside the task record** — consumers poll
`status(task_id)`. There is no result queue: polling-only (rule 8). There is no
scheduling queue and no `next_run_at`: nothing sleeps and nothing waits — a task
runs as soon as it is enqueued; scheduling (when a task may run) is a field on
the record, never a scheduler (rule 8).

**Surface.**

```
register(name, callable)                    # in-process: name → async fn | async generator
async submit_task(name, *args, **kwargs) -> task_id
async status(task_id) -> TaskRecord | None
async control_mode(task_id) -> str | None   # stored control instruction: run|pause|cancel|None
async pause(task_id) / resume(task_id)      # disable ≠ destroy: non-terminal suspension
async cancel(task_id)                       # terminal, cooperative
```

- The **callable registry is in-process code**, never a registry: it is the task
  executor's execution scope; state — the authority of what
  happened — is always a (persistent) registry on beaver.
- `control_mode` is the public read of the **cooperative control** dict (the
  instruction `pause`/`resume`/`cancel` the executor honors at each yield). The
  task record itself has no `paused` status (§6.1); the control instruction is
  the confirmable fact for the lifecycle verbs' by-reading confirm (§5.5/§5.6).
  `None` means no explicit instruction (equivalent to `run`).
- The Runtime and the agents **register their callables** with the Manager; to
  the Manager each is just a callable — it never sees what is inside. The
  callable is **not** the agent: the catalog records the agent by its own
  identity, with a separate `task_id` for the task the Manager manages (§4.8) —
  never a 1:1 instance ↔ task identity.

**Task-executor algorithm.** A Manager task executor is a polling loop over the
task queue:

```
1. pending_tasks.get(block=False) → dispatch   (IndexError ⇒ nothing due; run() returns)
2. dispatch(task_id):
   a. status := running (+ started_at)
   b. cancellable? drive the generator, checking control at each yield:
        run    → advance one step
        pause  → yield without advancing
        cancel → cancelling → failed(cancelled)
      not cancellable? await callable(*args, **kwargs)
   c. success + result | failed + error   (never silent, rule 9)
```

- **Executor death**: because `get()` is destructive/atomic and a task executes
  once, a crashed executor is not retried and runs are not at-least-once. A
  crashed task is surfaced visibly rather than silently re-run.
- **Multi-process concurrency**: several Manager task executors (any process)
  drain the same queue; `get()` is destructive/atomic, so a task executes once.
- **Executor occupancy (LEG-087):** a real bring-up is a parked async generator
  whose dispatcher awaits the agent's standing loop — that executor is
  dedicated to the agent for its whole life. A booted node therefore runs **one
  executor per live bring-up instance plus spares for facts** (§6.1); the spares
  drain `pending_tasks` exactly like the dedicated ones.

**Lifecycle verbs → task language.** The Manager executes these facts; the
Runtime decides them and records them in the `Registry` **after** each is
confirmed (registration-is-a-mirror):

| Runtime verb | Manager fact (task language) | Observable Manager state | Registry record (posteriori) |
|---|---|---|---|
| create_instance | `submit_task(...)` (bring-up task; real bring-up is a **parked async generator** spawning the agent's own standing loop, §5.2/§6.1) | `running` while the agent lives (structural terminal on its exit) | `record_instance` |
| destroy_instance | `DESTROY_INSTANCE` fact: **mints + deposits a signed `terminate_with_drain` control message** on the instance's class queue (LEG-082/LEG-087) | bring-up record reaches `success` — the agent's structural exit (no `cancel` on this path) | `remove_instance` |
| disable_instance | `DISABLE_INSTANCE` fact: **mints + deposits a signed `disable` control** on the class queue (the agent parks its own loop between dispatches) | converges on the state report (`parked`); bounded Registry read of `disabled` — no strong ack by design | `set_instance_state(disabled)` |
| enable_instance | `ENABLE_INSTANCE` fact: **mints + deposits a signed `enable` control** on the class queue | converges on the state report (`ready`); bounded Registry read of `enabled` — no strong ack | `set_instance_state(enabled)` |
| create_class (pool N) | N × `submit_task(...)` | N tasks `running` | `record_class` + N×`record_instance` + `cache_spec` |
| destroy_class | N × `DESTROY_INSTANCE` (terminate-with-drain deposits) | all bring-up records `success` | N×`remove_instance` + `remove_class` |

**What the Manager does NOT own** (decoupling boundaries):
- The **entry gate of a class queue** (what §0/§5.6/§5.8/§6 name "dispatch"):
  submitting into a disabled class is refused by the **Runtime** (it consults
  the catalog).
- The DAG, routing, delivery, results: all Runtime/agent concern.
- The Registry: the Manager never records anything in it.

**Compliance (audit).** Polling-only / no sleeps (rule 8); domain-free (rule 7 —
names/states only, never agents, never a task = agent-loop coupling); errors
never silent (rule 9 — `failed`+`error` visible, `cancelled`); everything is a
registry (rule 13 — callables are code, never authority); logging with the
implementation (rule 11); no instance supervisor (the Manager is executor only;
the Runtime decides); disable ≠ destroy (pause vs cancel); additive — the Runtime
and the agents are untouched. Implementation lands with the Runtime's lifecycle
ops (R-8), executed in task language by the Manager (§6.1).

---

## 7. Dependencies (from the YAML spec)

Dependencies come from the spec: a composite (via its `branches`) references
other classes by name. These govern enable/disable/destroy behavior.

- **Create** evaluates its **dependencies (down)**: a class is created enabled
  only if all dependencies exist and are enabled; otherwise born disabled.
  Creation does **not** create its dependencies in cascade.
- **Disable / destroy** cascade over its **dependents (up)**: disabling or
  destroying a class disables the classes that reference it (recursively — a
  dependency cascade / chain of disablement). The cascade is **transitive
  upward**: if something depends on it, that dependent is disabled, and anything
  that depends on *that dependent* is disabled too (it will not be able to
  resolve). Dependents become `created / disabled`, never destroyed by the
  cascade.
- **Destroy does not touch down**: the classes a destroyed class depends on are
  left untouched, because others may depend on them.
- **Re-creating a class does not re-enable its dependents** automatically: the
  system does not record *why* a disablement happened (by whom: a cascade vs an
  explicit operator action), so restoring a class does not undo explicit
  decisions. Re-enabling dependents is an explicit operator decision, guided by
  the live catalog (§9), which exposes the dependency graph so the upward chain
  can be deduced.
- **Cycles** in the dependency graph (a class depends transitively on itself)
  break the topological order — they are detected and must be resolved (rejected
  / flagged) rather than silently looping.

## 8. Bootstrap — build the initial state and the initial cache

The bootstrap **constructs the initial state of the system and the initial
cache** in the `Registry` (§0). It does **not** "run tasks": it leaves the
agents idle and the node standing. The catalog holds all dependency information
(classes, instances, DAGs and states), so the full graph is known. The node is
brought up **by CLI or programmatically** (the Runtime is the entry point); the
bootstrap runs the ordering below against the loaded catalog.

Consistent with registration-is-a-mirror, **nothing is recorded before it
happens**: each entry enters the initial catalog/cache only after the fact (the
agent was actually brought up / the spec was actually read) is confirmed.

1. **Runtime** loads the source specs (classes + pool of each).
2. Build the **topological order** of the dependency graph (leaves first, then
   the classes that depend on them, etc.).
3. For each class in that order: **Runtime** creates its queue (fact) →
   `record_class` + `cache_spec` (once, posteriori to the queue) → **Manager**
   submits the bring-up task for its agents one at a time (§6.1), each
   confirmed `running` before the **Registry** records the instance
   (§5.2/§4.8 ordering). (`pool_size` is the *intent*; the catalog reflects the
   *real* count of agents actually brought up — never a promise.)
4. Because each dependency is created (enabled) before its dependents, the
   enable-by-dependencies rule (§4.2) is satisfied naturally → dependents are
   created enabled.
5. Anything whose dependencies are not satisfied stays disabled and is visible
   in the catalog (its agents, if any, are recorded but stay disabled).
6. A dependency cycle breaks the order → detected and resolved (rejected /
   flagged).
7. The agents are already running their own internal loops (each was brought up
   in step 3, §6.1); the node's task executors are up and drain the Manager
   queue — one executor per live bring-up plus spares for facts (§6.1
   executor occupancy).

**Result:** the **initial catalog state** plus the **initial YAML cache** exist in
the `Registry`; agents are idle (nothing pending on their class queues);
**no business task has been started** (a task only begins when a work item
enters the starting agent's queue, Flows B/C in the execution model). The node
is ready to receive submits.

## 9. The live catalog (operator source of truth)

The catalog exposes classes, instances, DAGs and states (queues, enabled /
disabled, dependencies). The operator uses it to know what exists, what is
disabled and why, and to decide explicitly what to enable / re-enable / destroy.
No disablement-history is recorded; the live catalog is what guides decisions.

- **What the catalog exposes per class:** the class, its **direct dependencies**
  and its **dependents** (the inverse of the graph), its state, the instances it
  has, and the state of each instance. Exposed granularly via the queries in
  §4.8: `list_classes`, `class_dependencies`, `class_dependents`, `class_state`,
  `list_instances`.
- Exposing **dependents (the upward graph)** lets the operator deduce the
  cascade: when a class is disabled/destroyed, the whole transitive set of
  dependents is what got disabled, and re-enabling can walk that chain upward.
  (Even though the "by whom" is not recorded, the graph makes the candidates
  derivable.)

## 10. Open decisions and open risks

### Open decisions

1. **"Pending work" boundary / drain semantics:**
   `now` destroys everything including **pending** items (the
   operator consciously accepts the loss); `drain` waits for the queue to empty —
   in-flight steps run to completion, nothing new enters, and the
   drain completes. The queue does **not** grow, termination is eventual when
   things work, and a **large timeout** is the safety valve (visible failure on
   expiry — rule 9). No further boundary to decide.
2. **Destroy parameter default:** the default is **`drain`**
   (`--mode`/`{mode}` optional in CLI/HTTP, §4.8; §5.8). `now` is the explicit
   escalation. A `drain` that never ends is an operator-visible problem, never a
   silent hang (large timeout → visible failure, §4.6).
3. **Entry gate / dispatch ownership:** the class entry
   gate belongs to the **Runtime** — its submit path rejects new items against
   the catalog when the class is disabled. It is not a `Manager` fact (the
   Manager never sees business submissions, and there is no open/closed
   attribute on a beaver queue to "close"). §0/§5/§5.6/§5.8/§6 wording updated
   accordingly.
4. **Dispatch has no lease or reaper:** the agent's
   polling loop is not lease/supervised. `get()` is destructive/atomic: an item
   is popped once and routed, a crashed executor is surfaced visibly and never
   re-run (rule 8 polls, never pushes; rule 9 never silent). No reaper, no
   `executor_died` recovery, no retry/DLQ — those invented mechanism are removed.
5. **Manager scope names and placement:** the runtime
   triangle's one task environment is **`legio.manager`** (module and class
   `Manager` — the same name the shipped business, task surface already uses);
   its beaver scopes are `tasks` (dict), `pending_tasks` (queue), `control`
   (dict). Normative in `legio.naming` (LEG-016) at implementation. No
   `legio.taskmanager` module, no `tm_*` scopes, no `next_run_at` — those
   invented names are removed.

### Open risks

1. **The lifecycle layer must not absorb the DAG / routing** — it governs only
   existence and lifecycle. This is the central design risk.
2. **A disabled class, never re-enabled, accumulates or leaks pending work** —
   pending item loss on `now`-destroy is operator-visible/acceptable; `drain`
   avoids it. No silent accumulation is tolerated (rule 9).
3. **Two state levels (class + instance) must stay coherent** — prefer that
   instances read (derive) the class state rather than the registry coordinating
   each instance individually.
4. **"Polling" means the agent's loop is the agent's own `run()`** (LEG-023,
   bounded by `max_steps` — not a permanently active uncontrolled thread,
   AGENTS.md rule 8), not a leased, at-least-once execution and not a Manager
   task.
5. **A fact task can die outside any operator operation**
   (the executor crashes), leaving a catalog instance that no longer
   exists and a class that *reads* `enabled` (or `disabled`) with zero live
   agents — surfaced as a visible task failure (rule 9). The **effective state**
   read (§4.4) limits the damage — zero instances read as `disabled` — but the
   stale    entry persists until an operator `destroy_instance`, or the R-8 pools
   (LEG-080) reconcile/respawn it. Automatic reconciliation/respawn is **R-8 /
   LEG-080 pools** (not part of this design).

---

## 11. Reference comparison

- From `voice-notes-api`: an `AgentFactory.create_all_pools` walks the compiled
  pattern catalog at boot and creates a pool per pattern, each pool being N
  replicas over the same `db.queue(pattern_name)`. Unloading / dynamic loading
  does **not** exist there.
- legio **improves** on this: the same catalog walk happens at boot, but agents
  can also be **created on the fly** (with their queue) and **destroyed** at
  runtime — without restarting the node — via the four verbs above.
- Additional legio improvements over the reference:
  - **Pool size is a creation parameter** (0 ⇒ born disabled), extending the
    reference's fixed `pool_size` per pattern.
  - **Dynamic lifecycle** governed by create / enable / disable / destroy,
    including a dependency cascade, enable-by-dependencies, and the live catalog
    as the operator's source of truth.
- Confirmed principles taken forward from the reference:
  - The **queue belongs to the type** (class), not to an instance.
  - Multiple instances of a type share and consume the **same** queue.
  - The agent that starts a task concretizes and passes the DAG; atomic agents
    only advance by position in the passed token.

---

## 12. Execution mechanics and the lifecycle × execution handshake

> The token carries `level_route` (per-level), `current_index`,
> `end_of_level_queue`, `level`, `launcher_class`; the destination is by position
> + `level`, and the final result goes to the **final-result queue** (no store,
> no `client:` return).

This section specifies the **runtime flow of a task** and how it meets the
class lifecycle (create / enable / disable / destroy). It is the execution
counterpart of §0-§8, and it is written so the decoupling rules cannot be
re-learned wrong: the flow is **forward-only (a DAG, per level)**, state travels
**in the message** (Schema 2 token), and the only per-class data are a queue and
(where relevant) a gathering queue and a gate — never an out-of-message
accumulator.

### 12.1 The message payload is the state: it travels in the messages/token

Every agent is defined independently and declares, as part of its own identity
(Schema 1), an entry contract and an output contract, each naming an alias:

- `input_as` — the key under which the agent looks up its input in the payload
- `output_as` — the key under which the agent builds its output

Because each agent is defined on its own, its two aliases are its own and
differ from those of every other agent; nothing is inferred about them. The
message carries `payload` (the single container for both request and result
roles) and the flow fields (`level_route`, `current_index`,
`end_of_level_queue`, `level`, `launcher_class`, `task_id`, `branch_id`,
`message_type`, `schema_version`). There is **no** store that holds
per-agent/task state out of the token; the message payload is the state —
polling-only, nothing central, nothing staged out-of-message for later
combination. `root` lives in the FlowToken (subclassing the message), not in
the queue message.

The payload does **not** accumulate. Each agent receives the incoming payload,
reads only what it needs under its `input_as`, performs its processing, and
**builds its own output** that it sends under its `output_as`. Travelling from
one agent to the next is a **re-keying step**: the key under which the producer
sent its output is not the key the next consumer reads under, so whoever hands
the task to the next agent re-keys the produced value under the next agent's
`input_as`. The route (`level_route`) carries, per position, the class and its
`input_as` — the information the re-keying needs — while `output_as` stays
internal to its agent and never travels. The submit re-keys the client payload
under the first agent's `input_as` before handing it over. The payload is
therefore rebuilt at every hop, never extended.

The **contracts are verified at runtime on both edges**, uniformly, for every
agent — atomic or composite (rule 12). The declared `input_schema`/`output_schema`
are not decoration: before the step runs, the incoming data must **contain** the
`input_schema` — superset, the payload has the declared data and may carry more
(extras are irrelevant, never rejected) — and before the built payload is routed
(or, for a composite, before it resumes its level), the value under `output_as`
must contain the `output_schema`, also superset. Types are strict (a schema
`integer` never accepts `"3"`). A violation raises `ContractError` in the common
runner (`AgentBase`) and is routed as a visible `error` result, never silent
(rule 9). A payload that carries no `output_as` (an `error` result) passes
through the output check untouched; a composite's fan-in `EXECUTION_RESULT` is
internal gathering, not an entry, so the input contract applies to entry
requests only.

The **flow contract** (what the submit promises to the client) is anchored to
the **starting agent** (the chosen `main` agent the submit seeds the flow on):
its **input** is the `input_as` of the starting agent, and its **output** is
the `output_as` of the starting agent. Every other agent's `output_as` is an
internal bridge to a next step only; it is neither part of the client's request
nor of the final result. The submit re-keys the client payload under the
starting agent's `input_as`, and the flow ends when the starting agent (or the
last step of the level that it closes) delivers the result to the final-result
queue, built under the starting agent's `output_as`. For a composite starting
agent, its `output_as` is assembled by the composite from its branches (§12.4);
for an atomic starting agent, it is the agent's own `output_as`.

### 12.2 One inbox queue per class; every agent of the class polls it

- Every class has exactly **one inbox queue** (`legio:queue:<class>`); all of
  its agents (instances) poll and consume that same queue.
- The class inbox carries **entry requests** (`ExecutionRequestMessage`) and —
  since the authenticated control channel (LEG-082) — the node's signed
  `ControlMessage`s. Results are never deposited onto an inbox: they land on
  the closer's queue (`end_of_level_queue` — a final-result queue, or a
  composite's gathering queue, §12.3). **Partition is by queue, never by
  message type** for *results*; the one exception is the standing loop's
  `message_type == "control"` dispatch, which separates an order from a work
  item on the inbox (LEG-082).
- Agents are **interchangeable, stateless** consumers of the unit of work the
  class knows; an agent learns *what to do, where it is, and where the level
  ends* **only from the message + token** it just consumed (Schema 2: position
  in `level_route`, `level`, and the `end_of_level_queue` the flow creator
  assigned). There is no god agent, no per-task queue, no engine driving steps.
- The **agent's internal loop** (LEG-023, `AgentBase.run`) is the unit a
  brought-up agent executes per class: poll **one** due item from the class's
  inbox (`get(block=False)` → `IndexError` = idle, returns; rule 8) and process
  it, repeating until idle. The **standing loop** (LEG-082) is the live agent's
  long-lived variant: it suspends **in** its class queue (`get(block=True)` —
  the "agent suspends in its own queue" decision; beaver's producer-interleave
  yield is the queue's consumption mechanism, not an engine timer), honors an
  authenticated `ControlMessage` **between dispatches** — never inside a step,
  never by parking, leasing or task-ifying the loop — and its return **is** the
  agent's cooperative exit. The loop is the agent's own, the vehicle of the
  fact the Runtime orders; an agent is **not** a task and **not** a recorded
  Manager identity (its identity lives in the catalog, §4.8).

### 12.3 A composite has one gathering queue; branches route by position and level

The model has **one composite** (`type: composite` + `branches`, §4.10.4/§4.11),
not the two conceptual ones of the earlier model. There is no separate
"sequence" vs "parallel"; a composite with a single branch is a sequence, one
with several branches is a parallel — both are the same agent, the same runner.

- A composite consumes **two physical queues**: its class inbox
  (`legio:queue:<class>`, only entry requests, §12.2) and its own **gathering
  queue** (`legio:queue:gather:<agent_id>`, only its branches' fan-in results).
  The two are **separate beaver queues** — the gathering is not "the inbox
  viewed as a result channel". Partition is structural (by queue), so neither
  the base runner nor the composite ever inspects a message type to route work.
- Each **branch** is an ordered list of steps (each step a bare pattern-name
  reference); a branch of size 1 (an atomic) advances and closes; a branch
  whose steps reference a `type: composite` opens inner ramification. The
  step's `(class, input_as)` route is resolved by the loader when it builds the
  level/branch DAG (Schema 2).
- The composite's `end_of_level_queue`, in the tokens it fans out, **is** its
  gathering queue (`gather:<agent_id>`, Schema 2): branches return there by
  position/level.
- A composite is itself a class with its own queue/instances (§7, dependencies);
  it is invoked like any capability agent — through its inbox.
- There is no out-of-message accumulator; fan-in accounting lives in the
  composite's per-class state (§12.4).

### 12.4 The flow: one advancement rule, fan-out and fan-in

**Single advancement mechanism (Schema 2).** Every agent (atomic and composite)
implements the same continuation logic, forward-only **by position + level**
over the token:

```
advance(request) for the current step (level_route[current_index]):
  next_index = current_index + 1
  if next_index < len(level_route):
      # advance within the level's route (no next_queue field; position rules)
      deposit an ExecutionRequestMessage to class level_route[next_index],
        re-keying the produced value under level_route[next_index].input_as (§12.1)
  else:
      # end-of-level
      if level == 1:
          # FLOW END: end-of-sequence AND level 1 → final result
          deposit the final result to end_of_level_queue (= final-result queue)
          # nothing more is routed
      else:
          # branch close: return to the creator's gathering via end_of_level_queue
          deposit an ExecutionResultMessage(output|error) to end_of_level_queue
```

This is the **generalized end rule**: an agent is the flow's end
when it is the last of its `level_route` AND `level == 1`, regardless of whether it
is a branch step, an atomic, or a composite — not by any `client:` store
mechanism. `end_of_level_queue` is assigned by the flow creator (the **submit**
sets it to the final-result queue; a **composite** sets it to its own gathering
queue for its branches); the agent never decides the destination (Schema 2).

**Composite fan-out.** On receiving its request, a composite does **not** advance
while its branches run. It fans out: for each branch it deposits that branch's
**loader-resolved `level_route`** (its `(class, input_as)` steps) with
`current_index = 0`, `end_of_level_queue` = the composite's **gathering queue**,
`level + 1`, and a **fresh autogenerated `branch_id`** (unique — no parent-derived
naming — distinct from its own and from every other branch across all levels, to
avoid name collisions; constant along the branch's sequence, carried
on the message). It also re-keys the composite's shared `input_as` under the
first step's (resolved) `input_as` for that branch (§12.1).

**The composite's two-inlet poll (intake + gated collection).** A composite
consume its two physical queues (its inbox and its gathering, §12.3) in one
`process_next`, as two cycles:

1. **Intake (unconditional):** poll the class inbox (§12.2); every entry request
   is fanned out, and its task is annotated as **pending** in the composite's
   bookkeeping (rule 8 — nothing sleeps or waits; fan-out is a deposit).
2. **Collection (gated):** only when at least one task is pending is the
   gathering queue polled. With none pending there is nothing legitimate to
   receive, so poll it is pointless; a result that landed anyway is an
   **anomaly — surfaced visibly (rule 9)**, never processed silently and never
   re-deposited onto the gathering (no silent loop).

**Fan-in.** Each branch, on branch close, deposits its
`ExecutionResultMessage` to the gathering queue **conserving its `branch_id`**
(failures conserve it too — an `error` result, rule 9). The composite
contabilizes fan-in **one result per `branch_id`**: its gathering bookkeeping is
keyed by `(task_id, branch_id)`, under a lock. On **fan-in completion** (all of
the task's `branch_id`s accounted for) the composite **groups the branch results
by their `branch_id` slot** (each branch's built payload `{output_as: value}`
stays in its own autogenerated slot — collision-free even when two leaves share
the same `output_as`) and hands that grouped mapping to the pattern's own
**`build_output_as`** — the composite's output construction is its
implementation (§4.10.6); there is **no generic engine default** (the engine
cannot guess a shape validating against an arbitrary declared `output_schema`).
It then decrements `level` (−1) and
resumes its own level (`current_index + 1` → next of its level), with
`end_of_level_queue` the one its creator supplied. On this resume the composite
is just the current node again, so the token leaving it is re-keyed exactly like
any advance (§12.1): the composite's `output_as` is re-keyed under the next
step's (resolved) `input_as`. There is no special composite→output remap — the
fan-in builds the composite's own `output_as`, and the standard re-keying
applies when the token moves to the next node.
Composite-as-root (submit as the composite's creator) passes the composite's
route as level 1 with `end_of_level_queue` =
final-result queue; branches run at level 2; after fan-in it resumes its own
level-1 route with the final-result queue.

**Where does "am I finished/fan-in complete" live?** In the per-class
**gathering bookkeeping** of the composite (`state:<composite>`, keyed by
`(task_id, branch_id)`, under a lock, as in the reference), not in an
out-of-message accumulator and not in any central engine. A branch of size 1
with no ramification needs no such bookkeeping (§12.3).

**Failure and fan-in.** A child failure becomes an `ExecutionResultMessage`
with an `error` payload through the existing failure path (§7/ARCH §8, rule 9):
the composite accounts for it (conserving its `branch_id`) and the parent flow
continues or fails visibly. Errors are
never silent.

### 12.5 Handshake with the class lifecycle (deposit-time gate)

The one place execution and lifecycle meet is **deposit**: pushing work into
another class's queue. Rules (decoupled, local, no oracle):

1. **Nothing enters a non-enabled class** — neither a business submit (§6.1
   entry gate) nor an internal deposit by the advancement rule / fan-in.
   Enforcing at *consumption* is wrong: a disabled class keeps draining its
   pending work (policy A, §4.1/§4.6), so a message must never be allowed in.
2. **The gate is per-class data, not a central entity.** The class gate lives in
   one registry (`db.dict("gates")`, keyed by class = `{state: enabled|disabled}`)
   and is written **only** by the Runtime (in the lifecycle ops) and **read** by
   any depositor (a submit or an internal task). This is the same mirror
   principle as the catalog — local, named, no central authority deciding
   routing.
3. **Destroyed class = cleared queue + no gate.** beaver auto-creates a queue on
   `put` and offers no queue-deletion API, so a `put` to a destroyed class would
   silently rebuild an orphan queue and make the class look alive. `destroy`
   **clears the class queue and removes its gate row**; once the gate is gone,
   every deposit to that class is **blocked at the gate** (step 2) — no global
   oracle is needed to "know" the class died, and a `put` failure must never be
   relied on as an existence signal.
4. **Race at the threshold is accepted.** A deposit can win the race against a
   concurrent `disable`/`destroy`; the item then drains with the queue (policy
   A). This is best-effort by design, never a leak: the no-orphans invariant
   (§6) holds because every deposited message is either processed, visibly
   failed, or drained.
5. **A blocked deposit is a visible failure** through the failure mechanism
   (§12.4): if the gate is closed at deposit time, the step fails with an
   `error` result along its `end_of_level_queue` path (or failed task), never a
   silent no-op. **Exemptions**: an error-result deposit always proceeds
   regardless of the gate — a closed class keeps draining, and an error must
   always reach its return path.

### 12.6 Polling-only, fields not mechanisms (restated for execution)

- Scheduling is a field on the Manager task record, never a
  sleep/delay in the flow.
- Idle is `get(block=False)` → `IndexError` → the instance loop (the ephemeral
  `run`) returns. The standing loop (LEG-082) is the suspend-in-queue variant:
  a live agent waits **in its own class queue** rather than returning; its
  return is its own cooperative exit (a `terminate_with_drain` honored between
  dispatches).
- The instance loop is the agent's own (§6.1, §10 risks 3-5); the flow itself
  never sleeps, never loops waiting for a peer.
