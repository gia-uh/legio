# Changelog

All notable changes to `legio` are listed here per release. Each entry names
the merged issue (the `LEG-0xx` plan issue and its GitHub issue number where
the record is unambiguous), grouped by rasante (`docs/PLAN.md`).

## [0.1.0] - 2026-09-21

First release. `0.1.0` ships the complete decoupled, polling-only engine on
native beaver: Schema 1 patterns, the flow token & message model, composites
(unified runner), the Runtime triangle (Manager / Registry / Runtime), the
authenticated control channel with standing agent loops, pools, federation,
the CLI (`server`, `agent`, `validate`), the consumer guide, and the audit
hardening batch — a fully green gate (ruff check + format + pyright + tests).

### R-0 — Foundation

- **LEG-001** (#1) Repository & package bootstrap: layout, `pyproject.toml`,
  `uv` init, docs set, `.gitignore`.
- **LEG-002** (#2) CI pipeline: ruff + tests + typecheck; environment via `uv`.
- **LEG-003** (#3) Governance docs reviewed & frozen (AGENTS, CONTRIBUTING,
  ARCHITECTURE, PLAN, DEPENDENCIES, journal template).

### R-1 — Contracts v1 (specs approved before coding)

- **LEG-010** (#4) Patterns YAML schema (Schema 1): one agent spec per
  document, mandatory symmetric contracts, terse call vocabulary.
- **LEG-011** (#5) FlowToken & messages: fields, semantics, in-message route,
  `schema_version`, validators.
- **LEG-012** (#6) Primitives interface: Queue/Registry/Lock API over native
  beaver.
- **LEG-013** (#7) Tool registry interface (Schema 3).
- **LEG-014** (#8) Mini-manager contract: `submit`/`status` delivering the
  consumer contract.
- **LEG-015** (#9) Federation contract: symmetric catalog, work-item, outbox.
- **LEG-016** (#10) Naming & error conventions.
- **LEG-017** Security contract: two levels — shared federation token +
  per-request client tokens, HTTP surface protected.

### R-2 — Walking skeleton

- **LEG-020** (#12) Primitives over beaver (Queue/Registry/Lock wrapper).
- **LEG-021** (#13) Patterns loader (minimal): S1 YAML → typed agent models,
  branch resolution, reuse/encapsulation DAG.
- **LEG-022** (#14) ToolAgent + tool registry execution path.
- **LEG-023** (#15) AgentBase `run()`: polling-only dispatch loop.
- **LEG-024** (#16) Mini-manager: `submit`/`status` over the TaskRegistry.
- **LEG-025** (#17) REST surface over the mini-manager: `submit`/`status`.
- **LEG-026** (#18) E2E example: `transform` with a fake tool (domain-free).
- **LEG-027** (#19) Auth middleware enforcing the two security levels.

### R-3 — Atomics

- **LEG-030** (#20) LinguisticAgent with lingo (`LLM` + `eng.create`).
- **LEG-031** (#21) Structured output wiring + MockLLM tests.
- **LEG-032** (#22) Example `summarize` (linguistic, fake LLM).

### R-4 — Composites

- **LEG-040** & **LEG-041** Superseded by LEG-044 (unified composite runner);
  the old kind-based sequence/parallel split no longer exists.
- **LEG-042** (#25) Fan-in by branch slot (dedupe per `(composite, task,
  branch_id)`), `output_as` collision resolution.
- **LEG-043** (#26) Examples `extract_and_summarize` and `distribute_summary`
  on the unified composite runner.
- **LEG-044** Unify composites: one `type: composite` + `branches` model, a
  single composer runner (ramify → gather → build `output_as`).

### R-5 — Token

- **LEG-050** (#27) Root token authoring & root delivery (submit-seeded
  `end_of_level_queue`).
- **LEG-051** (#28) Uniform parent continuation in every composite frontier.
- **LEG-052** (#29) Fan-in identity by path (not agent name); `output_as`
  namespacing.
- **LEG-053** (#30) Final-result delivery semantics (branch-close vs flow-end).

### R-7 — Patterns engine

- **LEG-070** (#36) Cascade invalidation on invalid dependencies.
- **LEG-071** (#38) Dry-run validator + strict fail-fast startup:
  `legio validate --dry-run [--dir DIR | --config]` reports every invalid
  pattern and exits non-zero; boot refuses an invalid catalog.
- **LEG-072** (#42) Pattern compile: `output_schema` → validating pydantic
  models at the boundary.

### R-8 — Runtime

- **LEG-080** (#40) Pools: `pool_size` agents per class, n consumers on one
  shared class queue (exactly-once, #40 strict concurrency proof).
- **LEG-081** (#41) Runtime CLI (typer): `legio server` bootstrap +
  `legio agent` lifecycle verbs; SIGTERM drains in-flight work.
- **LEG-082** Authenticated control channel + standing agent loop:
  signed `ControlMessage`s minted only by the node's Runtime, honored between
  dispatches.
- **LEG-083** Manager generic task environment (`legio.manager`): submit /
  status / pause / resume / cancel with cooperative control.
- **LEG-084** Registry — the posterior mirror: `catalog` / `instances` /
  `yaml_cache`, granular reads, effective state.
- **LEG-085** Runtime public face: orchestrator + class entry gate, lifecycle
  verbs as "action → Manager fact → confirm → Registry record".
- **LEG-086** Runtime boot catalog: topological `create_from_catalog`, pool
  resolution precedence, cycle rejection.
- **LEG-087** Real bring-up (parked async generator) + lifecycle facts
  minting control; structural terminal states, no timers.
- **LEG-088** Node control intake (`node_ops`): the CLI/API/peer operator
  intent source, drained via Manager facts.
- **LEG-095** Instance state report intake: the control channel's out-side;
  per-agent result queues + outbox intake + node-injected db proxy.

### R-9 — Federation

- **LEG-090** (#43) Per-node catalog (roster derived from capacity, symmetric).
- **LEG-091** Step resolver: required agent → local | remote.
- **LEG-092** (#44) Work-item over HTTP + remote deposit, federation-token
  auth.
- **LEG-093** (#46) Outbox polling by the author (write-before-ack,
  idempotency).
- **LEG-094** Multi-node directed example (3 symmetric nodes, domain-free).

### R-10 — Hardening & release

- **LEG-100** Docs & examples hardening; glossary; consumer guide —
  executable top-to-bottom.
- **LEG-101** (#45) semver `0.1` + packaging + changelog + tags: this release,
  `uv build`, `CHANGELOG.md`, `git tag v0.1.0`.
- **LEG-102** Release-artifact validation: an in-repo harness that installs the
  built wheel into a throwaway venv and runs a headless smoke against it.
- **LEG-103** (#51) Audit hardening batch (sessions 87-88): coupling/polling
  model fixes, format-debt triage, strict concurrency coverage.