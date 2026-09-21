# legio glossary

Canonical definitions for the LEG-016 identifiers and the core architecture
terms. One definition per term, anchored to the code and `docs/ARCHITECTURE.md` /
`docs/AGENT_LIFECYCLE.md`. When a definition here and a code comment disagree,
the code wins — fix this glossary.

## The three schemas

- **Schema 1 (S1, pattern)** — the YAML `AgentSpec` one pattern per document:
  `name`, `type` (`atomic | composite`), `kind` (`tool | linguistic`, atomic
  only), mandatory symmetric `input`/`output` contracts
  (`input_as`/`output_as` + `input_schema`/`output_schema`), and by kind:
  `tool` + `parameters` (atomic tool), `prompt` (atomic linguistic), `branches`
  (composite). See `docs/AGENT_LIFECYCLE.md` §4.11 and
  `src/legio/patterns/schema1.py`.
- **Schema 2 (S2, token)** — the immutable `FlowToken` that travels between
  class queues: `level_route`, `current_index`, `end_of_level_queue`, `level`,
  `launcher_class`, `task_id`, `branch_id`, `message_type`, `payload`, `root`.
  Finality is derived from position, never stored.
  `src/legio/flow/token.py`.
- **Schema 3 (S3, tools)** — the `available_tools` declaration in `tools.yaml`:
  name → `implementation` (dotted path, resolved at runtime) + `policy`
  (`timeout`, `retries`). `src/legio/tools.py`, `src/legio/config.py`.

## Identifiers (LEG-016 naming contract, `src/legio/naming.py`)

- **agent id** — a pattern/agent name: `^[a-z][a-z0-9_-]*$`. Also the class
  name; `main: true` marks the submit entry point.
- **node id** — `^[^@]+@[^@]+$` (e.g. `transform@example`).
- **task id** — `agent_id:uuid` (`^[^:]+:[0-9a-f-]{36}$`); a client's work item.
- **queue_key(agent)** — the agent's inbox queue key (beaver queues spoken
  directly; no invented wrapper).
- **outbox_key(task)** — the task's final-result record queue/table (the
  `RESULT_DRAIN` intake collects into it; `/status` reads it).
- **result_queue_key(agent)** — the agent's shared final-result queue.
- **gathering_key(agent)** — a composite's fan-in collection queue.
- **branch_id** — a token's branch identity for composite fan-out
  (Schema 2; `FlowToken.branch_id`).

## Patterns, agents, lifecycle

- **atomic agent** — one standing agent with its own queue: a `tool` step that
  calls the registered tool, or a `linguistic` step that calls the injected LLM
  client. Boot-materialized ("standing").
- **composite agent** — a `type: composite` agent whose branches reference
  other agents by name (DAG over the catalog, leaves first). It has **no**
  generic output build — a concrete composite class implements
  `build_output_as` (the composite's output construction is the pattern's
  model). `src/legio/agents/composite_agent.py`.
- **class / instance** — the lifecycle (Schema 2 and §4.8 of
  `docs/AGENT_LIFECYCLE.md`): `create-class`/`destroy-class` manage classes
  (enabled/disabled; `0` pool ⇒ born disabled); instances are the message queues
  with `enable`/`disable`/`destroy` verbs. The catalog is the operator source of
  truth.
- **served / invalid catalog** — `Catalog.served()` = every loaded spec minus
  the invalid set; operators invalidate via `disable-class`. Composites require
  their concrete class injected at boot or the boot refuses loudly.
- **pool** — horizontal capacity intent per pattern (`pools` in `legio.yaml`:
  `per_pattern` > `per_kind` > `default` > 1). Capacity is deployment intent,
  not engine behavior.
- **standing vs capability** — agents boot once at node startup and stand;
  nothing is loaded dynamically at submit time (decoupled polling model).

## Engine & deployment

- **node** — one deployed database + config + standing agents: `legio.yaml`
  (`node`, `database`, `patterns`, `tools`, `services`, `pools`, `api`,
  `logging`, `lifecycle`, `federation`).
- **polling only** — the public API never pushes: managers/agents poll their
  own queues; nothing sleeps; scheduling is the `next_run_at` field.
- **executor pump** — the host drives `runtime.manager.run()` one pass per
  dispatch (never the Runtime); shutdown stops between dispatches.
- **bring-up / bootstrap** — `create_from_catalog` births the served classes
  leaves-first in topological order (the §8 bootstrap).
- **outbox** — the task result record (`outbox_key`); `/status` reads it; the
  `RESULT_DRAIN` intake collects completed root results into it.
- **NodeDB** — the beaver routing proxy wrapping the agents' db handle for
  federation (roster-based routing by `input_as`).
- **roster** — per-peer step map used by federation; a composite branch may
  reference a *known peer step* (`roster_steps`) — peers never widen scope
  (rule 9).
- **client token store** — `api.clients` / CLI `/status`/submit auth: name →
  bearer token registered at boot.
- **domain-free** — `legio` never knows a consumer domain; the only knobs are
  patterns (YAML as data) and the injected tool registry (rules 6/7).

## Errors (`src/legio/errors.py`)

- **LegioError** — base; every legio error carries a stable `code` derived from
  the message.
- **RecoverableError** — transient; may succeed on a later run.
- **UnrecoverableError** — fatal authoring/validation failure that surfaces
  loudly (never silent; rule 9). Loader shape rejections raise this (never a raw
  `ValidationError`).
- **InvalidNameError** — an identifier violates its naming contract
  (RecoverableError).

## References

- `docs/ARCHITECTURE.md`, `docs/AGENT_LIFECYCLE.md` (§4.8 verbs, §4.11 schemas),
  `docs/CONTRACTS/` (per-issue specs), `docs/CONSUMER_GUIDE.md` (walkthrough),
  `examples/` (runnable single-source examples).