# Consumer guide — adding tools + patterns to a node

This guide is **executable top-to-bottom**: every file it references lives in
`examples/` and is exercised by the test suite (`tests/test_leg100_consumer_guide.py`
walks this exact sequence, plus `tests/test_leg032_*` / `tests/test_leg043_*`
run the composite flows). If a step here drifts from the code, CI breaks.

The domain-free rule: patterns (YAML as data) and the injected tool registry
are the **only** knobs. Nothing in this repo knows a consumer domain
(AGENTS.md rule 7) — the examples use generic operation names on purpose.

## 0. Prerequisites

- Python 3.13 + `uv`.
- `uv tool install legio` (or `uv run legio ...` from the repo).
- For linguistic steps only: an LLM endpoint for `services.llm`.

Terminology you will meet is defined in `docs/GLOSSARY.md`.

## 1. Register a tool (Schema 3)

A tool is a plain callable; its signature is its contract. Declare it in a
`tools.yaml`:

```yaml
available_tools:
  transform:
    implementation: "examples.tools.transform"
    policy:
      timeout: 30
      retries: 0
```

`implementation` is a dotted path resolved at runtime
(`examples/tools.py` ships reference implementations — copy and swap them).
`policy` is `timeout`/`retries` (genuine numbers; bools/strings are rejected).

## 2. Write a pattern (Schema 1)

One YAML document per pattern. `examples/transform/patterns/tool/transform.yaml`:

```yaml
name: transform
type: atomic
kind: tool
input:
  input_as: transform
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
      factor: {type: integer}
output:
  output_as: transform
  output_type: json
  output_schema:
    type: object
    properties:
      transformed: {type: string}
tool: transform
parameters:
  text: "{transform.text}"
  factor: "{transform.factor}"
```

Parts: **name** (the agent id, `^[a-z][a-z0-9_-]*$`), **type/kind**, the
mandatory symmetric **input/output contracts** (`input_as` + `input_schema`,
`output_as` + `output_schema`), `tool:` (matches a declared tool name) and
`parameters:` (template placeholders `{<input_as>.<field>}`). Atomics also take
`prompt:` when `kind: linguistic`. Composites take `branches:` (a list of
step-name sequences) and `main: true` on the entry pattern.

Patterns sit in three per-type directories (`patterns/tool`,
`patterns/linguistic`, `patterns/composite`); all three must exist even when
empty — a missing directory is a loud boot failure, never a silent skip.

## 3. Configure the node (LEG-017)

`examples/transform/legio.yaml`:

```yaml
node:
  id: "transform@example"          # ^[^@]+@[^@]+$
database:
  db_path: "./db/transform.db"
patterns:
  tool: "./patterns/tool"
  linguistic: "./patterns/linguistic"
  composite: "./patterns/composite"
tools:
  config: "./tools.yaml"
lifecycle:
  default:
    drain_timeout: 10.0
    drain_interval: 0.02
```

Options: `pools` (capacity per pattern), `services.llm` (the LLM endpoint for
linguistic steps), `api` (host/port/clients), `federation` (peer nodes),
`logging`. Relative paths resolve against the process working directory — run
from the node directory.

## 4. Validate (fail fast at boot)

There is no separate validator command. Validation is the **boot-time
refusal**: `legio server` loads *and validates* the three pattern dirs, the
tools file and the config before any agent binds; any invalid pattern, missing
directory, bad tool declaration or bad config refuses the boot loudly, naming
the offender (rule 9). A wrong pattern never half-initializes a node.

## 5. Boot the node

```bash
cd examples/transform
legio server --config legio.yaml --host 127.0.0.1 --port 8000
```

The server materializes the standing agents, runs the §8 bring-up
(`create_from_catalog`, leaves first) and drives the executor pumps. The host
owns the pumps; the runtime never spawns work on its own (polling only).

## 6. Submit and read status

```bash
curl -s -X POST http://127.0.0.1:8000/submit \
  -H 'Content-Type: application/json' \
  -d '{"client_id":"demo","agent":"transform","payload":{"text":"hello","factor":2}}'
```

returns `{"task_id": "transform:<uuid>"}`. Poll:

```bash
curl -s "http://127.0.0.1:8000/status/<task_id>?client_id=demo"
```

until `"state": "completed"`, then read `output` (under the pattern's
`output_as`) and `result_key` (the outbox key, Schema 2). The final result
lands on the agent's shared final-result queue and is collected into the task's
outbox record — that is the whole return path; there are no push events.

## 7. Operate classes and instances

```bash
legio agent list-classes --config legio.yaml
legio agent class-state --class transform --config legio.yaml
legio agent create-class --class transform --pool 1 --config legio.yaml
legio agent destroy-class --class transform --config legio.yaml
```

Class/instance lifecycle verbs live in `docs/AGENT_LIFECYCLE.md` §4.8; the
catalog is the operator source of truth, and a `0` pool births a class disabled.

## 8. Composites and linguistic steps

The other three example nodes show the same skeleton extended:

- `examples/summarize/` — `summ` (linguistic) → `assess` (tool), one branch.
- `examples/extract-and-summarize/` — `extract` (linguistic) → `assess`, one branch.
- `examples/distribute-summary/` — `summ` + `cata` (two linguistic branches).

A composite's **output build is the pattern's model**: the engine has no
generic build — your composite class implements `build_output_as` and is
injected at boot (a composite without one refuses the boot, naming the agent).
Linguistic agents need `services.llm` in the node config. The example nodes
ship their own `legio.yaml`, `tools.yaml` and `patterns/`; `tests/test_leg032`
and `tests/test_leg043` run these flows end-to-end against the same files.

## 9. Proving the guide

`uv run pytest tests/test_leg100_consumer_guide.py` walks this guide literally:
every example pattern loads, every composite resolves, every config template
parses, and the `transform` node boots and serves submit → status over the REST
surface. Everything the guide shows is the same bytes the suite tests.