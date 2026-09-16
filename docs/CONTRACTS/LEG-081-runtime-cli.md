# LEG-081 — Runtime CLI (typer)

- **Status:** APPROVED (maintainer approval, session 86h; typer approved as a
  direct runtime dependency, session 86h)
- **Rasante:** R-8
- **GitHub issue:** #41
- **Source:** `docs/PLAN.md` (LEG-081)
- **Depends on:** LEG-014, LEG-025, LEG-027, LEG-017, LEG-015

## Goal
Operational entry points: the runtime bootstrap (`legio server ...`) brings up
the node's agents and serves `submit`/`status`, and `legio agent ...` exposes the
Runtime's lifecycle verbs (§4.8 of AGENT_LIFECYCLE). Honors the LEG-017 config
shape (node id, federation token, client tokens, peer allowlist). Domain
knowledge enters **only as YAML data** (patterns) — the CLI never invents or
hosts domain logic (rule: domain-free library).

## Scope
- **In scope:** typer CLI for the node + the agent lifecycle, config loading per
  LEG-017.
- **Out of scope:** validate subcommand (LEG-071), federation internals (R-9),
  business domains (legio sees only patterns as YAML data, rule 7).

## Contract & design
- `legio server --config path`: runs the bootstrap (§8 of AGENT_LIFECYCLE) —
  brings up the node's agents from their pattern specs — and serves
  `submit`/`status`; honors `api.clients` (LEG-027 middleware).
- `legio agent <command>`: maps 1:1 to the Runtime lifecycle verbs
  (create/enable/disable/destroy at the class and instance levels, §4.8) — a
  thin wrapper over the Runtime, no new logic.
- **`legio agent` transport (boot-in-process, approved session 86h):** control
  signing keys are per-boot, in-process, never persisted (LEG-082), so a
  separate process cannot mint valid control messages (2026-09-13 insight);
  each `legio agent` invocation therefore boots the node in-process from the
  same config (a fresh per-boot key + matching verifiers) and drives its own
  executor — the verb reaches the Runtime directly (`deposit_node_op` for
  enable/disable/destroy, direct verbs for create/recreate/reads). There is no
  lifecycle-over-HTTP surface (federation transports work, never lifecycle).
- **YAML is the data language of the lifecycle:** `create-class` takes the
  pattern spec as `<spec.yaml>` and `recreate-class` is driven by the cached
  YAML (`get_cached_spec`, §4.7); `--pool N` accompanies the spec at create
  (LEG-080). Read verbs (`list-classes`, `class-deps`, `class-state`,
  `list-instances`) delegate to the registry mirror.
- `legio server --config path --federation`: additionally exposes federation
  endpoints (once R-9 lands) with L1 federation-token guard + peer allowlist.
- Config YAML shape exactly per LEG-017 (`node`, `federation_token`,
  `api.clients[]`, `peers[]`).

## Config schema & environment (agreed baseline — Session 58)

Precedence (highest wins): built-in pydantic defaults < config file (argument /
`LEGIO_CONFIG` env / default `./legio.yaml`) < CLI overrides
(`--node`, `--db-path`, `--host/--port`, `--log-level`,
`--tool-dir/--linguistic-dir/--composite-dir`, `--tools`). Invalid values fail
loudly (`ConfigError`, unrecoverable) — the node never boots on a broken
config and secrets never travel through YAML (LEG-017 §2).

General config (`legio.yaml`, public part; secrets never in YAML — LEG-017 §2):

```yaml
node:
  id: "local@hostname"                 # LEG-016: <name>@<host>
database:
  db_path: "./data/legio.db"
patterns:                              # one dir per S1 pattern type; recursive *.yaml scan
  tool:       "./patterns/tool/"
  linguistic: "./patterns/linguistic/"
  composite:  "./patterns/composite/"
services:
  llm:                                 # lingo.LLM — model/api_key/base_url
    base_url: "http://127.0.0.1:1234/v1/"
    model: "qwen/qwen3-4b-2507"
  embedding:                           # lingo.Embedder — sibling service
    base_url: "http://127.0.0.1:1234/v1/"
    model: "text-embedding-3-small"
    max_tokens_per_batch: 8000         # OPTIONAL — absent → lingo default (8000)
api:
  host: "0.0.0.0"
  port: 8000
  clients:                             # LEG-017 §4 registry; tokens via env
    consumer-a: { agents: [flow_a] }   #   restricted; no agents → all starting agents
logging:
  level: "INFO"
  file: "./data/legio.log"             # empty → stream
tools:
  config: "./tools.yaml"               # pointer to the independent Schema 3 config
federation:                            # known peers of THIS node (outbound); admission = shared token only (LEG-017 §5)
  peers:
    - id: "prod-b@host-02"             # peer's node.id — whom this node knows / delegates to
      url: "http://host-02:8000"
    - id: "prod-c@host-03"
      url: "http://host-03:8000"
    - id: "prod-d@host-04"
      url: "http://host-04:8000"
```

Independent Schema 3 config (`tools.yaml`), validated with its own schema
(LEG-013); CLI override `--tools PATH`:

```yaml
available_tools:
  simple_transcription_tool:
    implementation: "consumer.tools.simple_transcription_tool"
    policy: { timeout: 120, retries: 3 }
```

Environment variables (secrets — LEG-017 §2, never logged):

| Variable | Role |
|---|---|
| `LEGIO_CONFIG` | path to the general config (default `./legio.yaml`; CLI `--config` wins) |
| `LEGIO_LLM_API_KEY` | api key for `services.llm` |
| `LEGIO_EMBEDDING_API_KEY` | api key for `services.embedding` |
| `LEGIO_FEDERATION_TOKEN` | shared node-to-node secret (LEG-017 §2/§5) |
| `LEGIO_CLIENT_TOKEN_<NAME>` | per-system client token; `<NAME>` = a key of `api.clients` |

## Interface
- Two typer commands; config schema per LEG-017 (extended by the sections above).

## Runtime — Materializer & node boot (implemented — Session 59)

Outside the typer CLI (which awaits dependency approval), the node boot is
already wired from `LoadConfig` — this is the engine piece that turns a
documented config into a running, observable node:

1. `legio.config.load()` resolves the file + env + CLI overrides (Session 58).
2. `boot_node(loaded, *, db=None, lingo_factory=None, composite_classes=None,
   on_built=None)`:
   - connects the database (opens the configured path unless `db` is injected)
     and builds a `Runtime(db, node_id=cfg.node.id)` so every scheduled task
     carries the configured node id as its origin;
   - loads the three pattern dirs (`load_pattern_dirs`, recursive `*.yaml`)
     into one Catalog and **validates before anything binds**: a missing dir
     or an invalid pattern refuses the boot (rule 9);
   - loads the independent Schema 3 `tools.yaml` into the tool registry;
     **fail-fast by design**: a `tools.yaml` that cannot be read is a
     `ConfigError` that refuses the whole boot, even for a catalog declaring no
     tools — unlike the general config's tolerant built-in default, the tools
     pointer is never silently empty (rule 9; tools are the node's declared
     capability surface). An explicitly empty `available_tools: {}` file is the
     correct way to boot a tools-free node;
   - materializes the standing agent map in DAG order — tool/linguistic atoms
     first, composites after, from the resolved LEG-070 branches (tooling uses
     `legio.patterns.compile.compile_schema` for the linguistic `output_model`);
   - builds the client store from `LEGIO_CLIENT_TOKEN_<NAME>` only
     (token-less clients are skipped with a warning — LEG-017 §2);
   - returns a `BootedNode` exposing `config`, `agents`, `catalog`, `db`,
     `runtime` (the `Runtime`), `client_store`, `app`
     (`create_app(runtime=..., clients=..., pattern_catalog=...)`)
     and `starting_agents` (the `main: true` specs the supervisor polls).
   - Resource seams are injected (rule 7): `db`, the tool registry,
     `lingo_factory` (default = legio builds `lingo.LLM(model, base_url, api_key)`
     from `services.llm` + env), and concrete composite classes. Unmaterializable
     specs — undeclared tool, linguistic agent without an LLM service/factory,
      composite without a concrete class — fail the boot loudly, naming the
      agent. Nothing sleeps and nothing is pushed: polling-only (rule 8), the
      agent loops and the Manager's executor drain their queues on demand.

## Acceptance criteria
From `docs/PLAN.md` (LEG-081), verbatim:
- The node starts via the CLI, exposes submit/status, and honors the LEG-017
  config shape; the lifecycle verbs are reachable through `legio agent`.

## Tests
- CLI contract tests: start/stop, config loading, endpoint exposure, lifecycle
  verb mapping.

## Validation case
- The E2E example run through the CLI.

## Definition of done
- All acceptance criteria met by running checks.
- Maintainer approval recorded; the maintainer closes the GitHub issue.
- Journal entry appended.