# PLAN — Development plan by verifiable issues

Process: **plan with issues → specs (approved) → implementation**. Every issue
below carries an explicit, testable acceptance criterion (marked **Accept**).
An issue is *done* only when its criterion is demonstrably met by a running
check (test, command, or CI job) — not by eyeballing. Each issue has its spec
in `docs/CONTRACTS/LEG-xxx-*.md`, and the GitHub issue links to that file. The
GitHub issues in this repo mirror this document one-to-one (labels R-0..R-10).

Read `docs/ARCHITECTURE.md` for the design these issues implement, and
`docs/CONTRIBUTING.md` for the methodology (contract-first TDD, dogfooding,
vertical slices).

## Rasantes (vertical slices)

`R-0` Foundation → `R-1` Contracts v1 → `R-2` Walking skeleton → `R-3` Atomics
→ `R-4` Composites → `R-5` Token → `R-7` Patterns engine → `R-8` Runtime →
`R-9` Federation → `R-10` Hardening & release.

Per-rasante definition of done: written contract + contract tests + implementation
+ validation case green (an external consumer repo or the in-repo fictitious
domain) + docs + journal entry.

## Issues

### R-0 — Foundation

- **LEG-001** Repository & package bootstrap: layout, `pyproject.toml`, `uv`
  init, docs set (this series), `.gitignore`.
  - **Accept**: `uv run python -c "import legio"` works; `uv lock` resolves
    against the approved dependency set only; package builds.
- **LEG-002** CI pipeline: ruff + tests + typecheck; environment via `uv`.
  - **Accept**: a PR triggers one pipeline running `uv run ruff check`,
    `uv run pytest`, and typecheck; a failing test turns the job red and a
    passing one green. No real network/LLM in CI.
- **LEG-003** Governance docs reviewed & frozen (AGENTS, CONTRIBUTING,
  ARCHITECTURE, PLAN, DEPENDENCIES, journal template).
  - **Accept**: every doc cross-references only existing files; all issue
    numbers mentioned in docs exist in this PLAN; an approval is recorded in
    the journal.

### R-1 — Contracts v1 (specs, approved before coding)

Each contract issue produces a spec document in `docs/CONTRACTS/` plus the
contract test file that fixes it (red before implementation). **Accept** for
every contract issue: spec approved (journal) and its contract tests exist and
are red for the yet-unimplemented surface.

- **LEG-010** Patterns YAML schema (Schema 1): **one agent spec**
  (`docs/AGENT_LIFECYCLE.md` §4.10/§4.11 Schema 1), domain-free.
  A pattern is YAML data; it declares **agents** only. `type` (atomic|composite)
  × `kind` (tool|linguistic, atomic-only; a composite carries `branches`, no
  `kind`), structurally enforced; **mandatory symmetric entry/output contracts**
  (`input_as`/`input_type`/`input_schema`,
  `output_as`/`output_type`/`output_schema`; text/json/binary) on **every**
  agent; the **terse call vocabulary** `parameters: {arg: dotted.path |
  literal}` for `kind: tool` (no `{from:}`/`{value:}`/`{default:}`, no `bind:`,
  no `emit:` — those are rejected); explicit `{input_as}.{key}` paths (the
  agent's own `input_as` + a key of its `input_schema`, AGENT_LIFECYCLE §4.12);
  interior↔contract coherence (tool
  `parameters` ↔ registered signature, checked at load; linguistic prompt
  variables ↔ `input_schema`, all used/all declared; linguistic `output_schema`
  enforced at runtime); reuse of any definition by `pattern:` and by repetition
  by position, with encapsulation, `output_as` uniqueness and cycle rejection;
  composite output is the composite's **implementation** (no `emit:`, no "last
  child renamed"); composite children bind only from the composite's `input_as`;
  `main` as root capability, not position. Composition is **contract
  compatibility**, not exact subset. Read semantics (single-node validation,
  `docs/VALIDATIONS/single-node-model.md`): steps read the value **re-keyed**
  under their own `input_as`; `parameters` are explicit `{input_as}.{key}`,
  never resolved implicitly; the payload is **built** across steps (construction
  + re-keying, no accumulation).
  - **Accept**: a fixture translating two representative composite patterns
    into S1 YAML loads and validates (a single-branch composite reusing a tool
    node and a linguistic node; a multi-branch composite with two branches); a
    tool `parameters`
    path `{input_as}.{key}` resolves against the value re-keyed under the
    agent's own `input_as` (a 3-step chain proves it) and fails on
    undeclared aliases/paths; a `{var}` in a linguistic
    prompt not declared in `input_schema` is a load error; nodes missing either
    contract (`input_as`/`input_type`/`input_schema` or
    `output_as`/`output_type`/`output_schema`) are rejected; the same agent
    definition reused in two composites validates in both; a use that violates
    the used agent's entry contract or adds an undeclared key is rejected;
    tool/linguistic coherence violations are detectable; duplicate `output_as`
    in one scope and composition cycles are rejected.
- **LEG-011** FlowToken & messages: fields, semantics, `schema_version`,
  root handling, delivery.
  - **Accept**: serialization/deserialization round-trip preserves all fields;
  versioned, rejected on major mismatch; finality derived from position.
- **LEG-012** Primitives interface: Queue/Registry/Lock API over native beaver.
  No `legio.primitives` wrapper; beaver primitives are addressed directly
  (`docs/ARCHITECTURE.md` §2).
  - **Accept**: signature conformance tests against the contract;
    queue/lock semantics observable by test.
- **LEG-013** Tool registry interface (Schema 3): the tools
  declaration `available_tools: {<name>: {implementation: <dotted.path>,
  policy: {timeout, retries}}}`. Each tool is an independent, autosufficient
  resource; it does **not** declare its output capacity (consuming agents
  declare it via `output_as`/`output_schema`); its identifier is the explicit
  `available_tools` key bridged by the agent's `tool: <name>` (Schema 1);
  verification against the tool (its signature/parameters) is **execution-time**
  (dynamically loaded), while load-time verification is the flow-against-itself.
  - **Accept**: (S1/S3 fixtures) `available_tools` with
    `implementation`+`policy` loads and validates; a broken/missing
    `implementation` fails loudly at execution, never silently; a tool whose
    signature rejects an agent's `parameters` call is a visible execution error.
- **LEG-014** Mini-manager contract (Schema 2): `submit`/`status` delivering the
  root result to the task's final-result queue, client token ownership tagging
  (per LEG-017).
  - **Accept**: submit records `task_id` + `client_id`; status scopes to the
    requesting `client_id`.
- **LEG-015** Federation contract: symmetric catalog, work-item, outbox
  with versioned interfaces.
  - **Accept**: contract tests cover read-before-write, idempotency, and
    interface/schema mismatch returning 4xx.
- **LEG-016** Naming & error conventions: queue/scope namespacing,
  namespace prefixes, error model.
  - **Accept**: a check (lint/unit) verifies every producer uses the
    `legio:queue:` prefix / `db.dict` scope and error types conform.
- **LEG-017** Security contract: two levels — shared federation token +
  per-system client tokens with individual revocation; default all starting
  agents unless restricted; ownership of task results.
  - **Accept**: contract tests confirm wrong/missing token → 401/403; a
    revoked client token is rejected immediately while others keep working;
    a restricted token cannot submit unlisted starting agents.

### R-2 — Walking skeleton

- **LEG-020** Primitives over beaver (Queue/Registry/Lock wrapper).
  The agent speaks beaver natively and directly — no wrapper implementation.
  - **Accept**: the AgentBase runner consumes from a native beaver queue (no
    wrapper), exercising priority ordering and namespace isolation directly
    against a temp beaver file.
- **LEG-021** Patterns loader (minimal): S1 YAML → typed agent models.
  - **Accept**: minimal atomic (tool/linguistic) and single-/multi-branch
    composites in the S1 shape load; invalid YAML raises a structured loader error
    (fail-fast); the branch-exclusive and mandatory-contract validation matrix
    (per LEG-010/Schema 1) is enforced at load.
- **LEG-022** ToolAgent + tool registry execution path (Schema 1/2/3).
  - **Accept**: a `kind: tool` agent executes against its `tool: <name>` in
    `available_tools`, resolving `parameters` (dotted paths/literals) into the
    call, validating input/output contracts on the edges, advancing the route by
    position (Schema 2) and depositing the new payload; failures yield a
    visible error in the task result.
- **LEG-023** AgentBase `run()`: polling-only dispatch loop (Schema 2).
  - **Accept**: dispatch is stateless and carries no lease — a message is popped
    once and routed; no infinite busy loop.
- **LEG-024** Mini-manager: `submit`/`status` over the TaskRegistry + client
  token ownership (LEG-014/017).
  - **Accept**: submitting with token A creates a task owned by A; status with
    a different token (or none) is rejected/empty.
- **LEG-025** REST surface over the mini-manager: `submit`/`status` with
  ownership-aware status; an agent polls its queue and runs to completion.
  - **Accept**: submitting via the API creates a task owned by the client; a
    foreign client's status request is denied; the agent loop processes the
    deposited message to completion with observable state changes in registries.
- **LEG-026** E2E example: `transform` with a fake tool (domain-free).
  - **Accept**: submitting `transform` through the API yields its output
    in the agent's shared final-result queue (`result:<agent>`, collected into
    the task's outbox record, read back via `status`); example is a green test
    (does not bitrot).
- **LEG-027** Auth middleware: single middleware enforcing the
  LEG-017 endpoint→token map for the API surface.
  - **Accept**: middleware tests match the LEG-017 §9 contract list for
    `submit`/`status`.

### R-3 — Atomics

- **LEG-030** LinguisticAgent with lingo (`LLM` + `eng.create`).
  - **Accept**: linguistic pattern produces structured output via lingo with
    `MockLLM`; schedule recorded only when actually needed.
- **LEG-031** Structured output wiring + MockLLM tests.
  - **Accept**: malformed LLM output raises a visible structured error, never
    silent; MockLLM fixture drives golden tests.
- **LEG-032** Example `summarize` (linguistic, fake LLM).
  - **Accept**: `summarize` is a green test using MockLLM end-to-end.

### R-4 — Composites

> **In progress (current variation):** unify composites — see LEG-044 below. This
> supersedes LEG-040/041/042/043's separate kind-based sequence/parallel model.

- **LEG-044** Unify composites: replace the kind-based sequence/parallel split
  with a single
  `composite` carrying `branches`. A `composite` with 1 branch is a sequence, >1
  a parallel; each branch is a route/sequence of agents (atomic — a sequence of
  size 1 — or composite, any cardinality, recursive). One composite runner
  (ramify → gather → build its `output_as`); branch fan-out deposits each
  branch's **own** `level_route` (not a single class), so a branch may be
  atomic / sequence / composite. Construction + re-keying model (Session 20);
  flow contract = starting agent's `input_as`/`output_as`.
  - **Closed vocabulary (Session 23-24 discussion):** `name` mandatory in every
    agent; `type: atomic|composite`; `kind` only `tool|linguistic` (atomic
    interior); a composite is `type: composite` + `branches`, no `kind`. Every
    agent declares the full entry + output triples
    `(input_as/input_type/input_schema, output_as/output_type/output_schema)`.
    `parameters` dotted paths are **mandatorily explicit** `{input_as}.{key}`
    (the agent's own `input_as` + a key of its `input_schema`); no implicit
    resolution. **No inline definitions, no nested `branches`, and a step in a
    composite is a bare pattern name — it carries no `input_as`/`output_as`.**
    The `(class, input_as)` pair a route needs is **not declared in the agent
    definition**: the loader resolves it when it builds the DAG of each
    level/branch (Schema 2). Inner branching is a step whose `pattern` is a
    `type: composite`.
  - **Plan**: `docs/JOURNALS/2026-09-03.md` Session 22 (Fase 0-4). Docs first
    (AGENTS.md rule 12), then code, one authorized step at a time.
  - **Accept**: examples behave identically under the unified model; nested
    composite branches (multi-step, composite-in-branch) run correctly; docs
    re-written first; suite green.
- **LEG-040** → **superseded by LEG-044** (unified composite runner). Contract
  rewritten to the unified model. The old dedicated sequence/parallel runner
  split no longer exists (one composite runner).
- **LEG-041** → **superseded by LEG-044** (unified composite runner). Contract
  deleted; content absorbed into LEG-040. No dedicated parallel runner exists;
  multi-branch composites cover it.
- **LEG-042** Fan-in by branch slot (dedupe per `(composite, task, branch_id)`).
  - **Accept**: two occurrences of the same child pattern in one DAG are
    distinct tasks (dedupe per `(composite, task, branch_id)`, not by agent
    name); the composite builds its payload from branch results; collisions
    resolved by `output_as`.
- **LEG-043** Examples `extract_and_summarize` (single-branch composite) and
  `distribute_summary` (multi-branch composite), domain-free.
  - **Accept**: both are green tests exercising real composite flows; both use
    the same unified composite runner.

### R-5 — Token

- **LEG-050** Root token authoring & root delivery (submit-seeded
  `end_of_level_queue` = the task's final-result queue).
  - **Accept**: root task result lands in the final-result queue exactly once and
    is readable via status; no re-delivery after ack.
- **LEG-051** Uniform parent continuation in every composite frontier.
  - **Accept**: single-branch and multi-branch composites return to the exact
    parent that deposited them; nested composite returns correctly (tested with
    a composite-inside-composite).
- **LEG-052** Fan-in identity by path (not agent name); `output_as`
  namespacing fixes.
  - **Accept**: same-named branches at different positions do not
    join together; regression tests cover the earlier agent-name bug.
- **LEG-053** Final-result delivery semantics (branch-close vs flow-end).
  - **Accept**: the flow-end at `level == 1` delivers the final result to the
    submit-seeded final-result queue, distinct from intermediate branch closes;
    covered by contract tests.

### R-7 — Patterns engine

- **LEG-070** Cascade invalidation on invalid dependencies.
  - **Accept**: disabling one broken pattern transitively disables dependents;
  the catalog reflects it; test verifies the full chain.
- **LEG-071** Dry-run validator + strict fail-fast startup.
  - **Accept**: `legio validate --dry-run` reports every invalid pattern and
    exits non-zero; startup refuses to serve a catalog with any invalid
    pattern.
- **LEG-072** Pattern compile: `output_schema` → pydantic models.
  - **Accept**: unions, arrays, nested objects, and recursive schemas compile
    to validating pydantic models (H4); a compiled schema rejects a bad
    payload at the boundary.

### R-8 — Runtime

- **Agent lifecycle** (create / enable / disable / destroy at the Class and
  Instance levels, including dynamic on-the-fly creation and the "disabled ⇒ no
  instances / destroy class = armageddon" rules) is specified in
  `docs/AGENT_LIFECYCLE.md`. This R-8 section is where it is implemented; LEG-080
  (pools) realizes "multiple instances consume the same class queue".
- **LEG-080** Pools (`pool_size` agents per class).
  - **Accept**: n agents on the same queue process n items concurrently;
    single-agent behavior unchanged.
- **LEG-081** Runtime CLI (typer): node bootstrap + `legio agent` lifecycle
  verbs (config: node id, federation token, client tokens, peer allowlist).
  - **Accept**: `legio server ...` starts, exposes `submit`/`status`, and honors
    the LEG-017 config shape; the Runtime lifecycle verbs are reachable via
    `legio agent ...`.
  - **Accept**: a `SIGTERM` drains in-flight work before exit — the CLI stops
    driving the executor between dispatches (LEG-083/LEG-085 one-pass run) and
    an in-flight step completes before exit. No engine-side shutdown seams:
    the engine owns no shared resource beyond the queues (beaver's own
    consumption mechanism), and `process_next` is one atomic step — pull,
    handle, deposit — so shut-down is naturally the host's no-dispatch
    decision.
- **LEG-083** Manager generic task environment (`legio.manager` module + class
  `Manager`, per `docs/AGENT_LIFECYCLE.md` §6.1). Additive to the Runtime and
  the agents. Reifies the docs' runtime-triangle slice: generic task submit /
  status / pause / resume / cancel with cooperative control, in-process callable
  registry, task-executor polling loop over `pending_tasks` (priority 0).
  - **Accept**: any kind of task is managed (submit→run→success|failed, pause
    non-terminal, cancel terminal `failed(cancelled)`); beaver footprint is
    exactly `tasks` / `pending_tasks` / `control` with no result queue, no
    scheduling queue and no `next_run_at`; task ids are `<node_id>:<uuid>` and
    never `instance_id`; a task is not an agent cycle.
- **LEG-084** Registry — the posterior mirror (§0/§4.8): the live catalog
  (classes / instances / dependencies) and the runtime YAML cache (§4.7),
  recorded after the facts occur; granular read queries with derived effective
  state (§4.4). Operational, never initiating; chooses never.
  - **Accept**: footprint exactly `catalog` / `instances` / `yaml_cache`;
    record_class+cache_spec before record_instance (no orphan instances);
    effective-state reads (stored enabled + ≥1 recorded instance); idempotent
    record/remove, rule-9 errors on set/query of non-existent entries;
    instance_id is the agent's identity, never a task_id.
- **LEG-085** Runtime public face (`legio.runtime.Runtime`): the orchestrator
  and translator between Manager task facts and Registry catalog facts (§0/§6).
  Owns the class entry gate (§12.5); re-homes the business `submit`/`status`
  under its node identity; implements the §5 lifecycle verbs (create / recreate
  / enable / disable / destroy at both levels) as "action → Manager fact →
  confirm by Manager read → Registry records, posteriori".
  - **Accept**: footprint — the Runtime owns only `gates`; the Manager holds
    `tasks`/`pending_tasks`/`control` (including the business `seed` task it
    mints through `submit_task`), the Registry holds `catalog`/`instances`/
    `yaml_cache`, all reached only through their public APIs (no layer writes
    another layer's scope, pinning tests); every §6.1 translate-table line has
    a test; terminal confirm on destroy (one-shot bring-up SUCCESS /
    `failed(cancelled)` on a live cancel); the gate blocks submits into a
    disabled class before anything is minted; business task ids are
    `<node_id>:<uuid>`; the Runtime does not pump (the node drives the
    executor; no `run()`/`register()`).
- **LEG-086** Runtime boot catalog (§8, pools wiring of LEG-080):
  `create_from_catalog` brings the initial catalog state up in a topological
  order (leaves first) so dependents are created enabled (§4.2/§8 step 4),
  resolving each class's pool as explicit `--pool N` (invocation) >
  `per_pattern` > `per_kind` > `default` > 1 (§4.3); `0` borns the class
  disabled; a dependency cycle is rejected before anything is recorded (§8
  step 6); `spec_yamls` feed the runtime YAML cache (§8 step 3). Pool size is
  intent — the catalog records the real count of instances brought up.
- **Accept**: dependents born enabled from a satisfying graph; pool resolution
     precedence tested; cycle rejection leaves no recorded class; per-class
     instances reflect the resolved pool. The "single-queue concurrent processing"
     interior of LEG-080 is resolved by the standing agent loop of LEG-082
     (N instance loops, one class queue, asyncio interleave; maintainer decision,
     session 85m).
- **LEG-082** Authenticated control channel + standing agent loop. The **only**
  way to talk to an agent is its queue: lifecycle orders arrive as signed
  `ControlMessage`s minted **only** by the node's Runtime and honored by the
  agent **between its dispatches**. Replaces the Manager control-mode coupling
  of LEG-083 for instances.
  - **Accept**: `legio.flow.control` — `ControlMessage` (frozen: target_instance,
    action `enable|disable|terminate_with_drain`, origin `operator|automatic`,
    seq, HMAC signature); per-boot in-process node key (never persisted);
    verifier is a pure function the agent holds (verify-only, cannot mint);
    control deposits use priority `-1.0` (beaver `ORDER BY priority ASC`, work
    stays `0.0`) so control jumps work; `seq` monotonic per instance (anti-replay,
    restart resets the key so old signatures never validate).
  - **Accept**: `AgentBase.standing_loop()` — long-lived interior loop that
    suspends on the class queue (beaver `get(block=True)`, the sanctioned agent
    suspension); an item whose `message_type == control` is validated (None
    verifier → visible WARNING, never silent) and honored **between dispatches**:
    `enable` resumes consuming, `disable` parks locally (loop alive, work
    requeued at the back), `terminate_with_drain` finishes in-flight work then
    returns (the agent exits its own loop); a control addressed to another
    instance of the pool is requeued at the back (best-effort, §12.5.4-style
    race accepted); `run()`/`process_next()` unchanged. The class inbox now
    carries control messages too (§12.2 amended); results still partition by
    queue.
- **LEG-087** Real bring-up (parked async generator) + lifecycle facts minting
  control. The one-shot bring-up seam becomes the real one: an **async
  generator** registered with the Manager (parked by `_drive_parked`), which
  spawns the instance's `standing_loop` and yields at between-dispatch
  checkpoints; the record reads `running` while the generator is parked and a
  terminal state only when the agent exited its own loop (`finally: await
  loop_task`).
  - **Accept**: the Manager knows an agent stopped **structurally** — no timers,
    no polls; the bring-up confirm waits `running` (§5.1 step 2, updated), not
    SUCCESS; instance `enable`/`disable`/`destroy` verbs ride new lifecycle
    facts that mint the signed `ControlMessage` deposit on the target instance's
    queue, confirmed by bounded Manager reads (§5.8); destroy no longer uses
    `manager.cancel` — the message ends the agent and the record terminal
    follows; per-agent control verifiers injected at boot; boot registers the
    real bring-up generator (multi-executor per §6.1).
- **LEG-088** Node control intake (`node_ops`) + Runtime wiring. The Runtime's
  own intake queue for operator intents (CLI/API/peer) — the **source** of the
  `origin: operator` lifespan, drained **via Manager facts**, never pumped by
  the Runtime.
  - **Accept**: the Runtime's footprint extends from only `gates` to
    `gates` + the `node_ops` intake (owner/signer of every control message, the
    only minter); intents → Runtime decides → Manager fact mints + deposits;
    naming never collides with the Manager's `control` scope; the CLI (LEG-081)
    reaches the verbs through `node_ops`.
- **LEG-095** Instance state report (`state_report` intake) — the control
  channel's **out-side**. The agent deposits an unsigned `AgentStateReport`
  (instance, action, seq of the honored control, resulting state) on a
  node-internal intake queue; a Runtime fact validates it by correlation with
  its own pending mint ledger and only then writes Registry/gate state. Removes
  the optimistic posteriori writes and the "no ack by design" clauses in LEG-087
  (§5.8) / AGENT_LIFECYCLE §5.3/§5.5-§5.6; `destroy` confirm stays structural.
  - **Accept**: `enable_instance`/`disable_instance` confirm only after the
    matching report is consumed and the Registry reads the target state (bounded
    read, no timers elsewhere); a control is honored → exactly one report with
    its `seq`; a rejected/foreign control emits none; a report without a
    pending mint is a visible WARNING, never applied; missing report within the
    budget → visible `RecoverableError`, never a silent state.
  - **Accept (Phase 2 — per-agent result queue + outbox intake, approved
    session 86b)**: `submit` seeds `end_of_level_queue == result:<starting-agent>`
    (one queue per root agent, shared across tasks); the `RESULT_DRAIN` intake
    collects flow-end results into per-task `outbox` records; `status` /
    `read_outbox` / `ack_outbox` read records, never the physical queue; an
    invalid item on a result queue is a visible WARNING, consumed, never
    applied; `destroy_class` clears `result:<name>`.

### R-9 — Federation

- **LEG-090** Per-node catalog (roster derived from capacity, symmetric).
  - **Accept**: catalog served over `GET /catalog` lists agents, interfaces and
    `schema_version`; requester token (federation) validated.
- **LEG-091** Step resolver: required agent → local | remote.
  - **Accept**: author resolves each step locally when present, remote when the
    peer catalog offers it, error otherwise; resolution happens before deposit.
- **LEG-092** Work-item over HTTP + remote deposit, federation-token auth
  (LEG-015/017).
  - **Accept**: `POST /work-items/{agent}` with valid federation token and
    matching interface deposits into the acceptor queue; mismatch → 4xx;
    no token → 401.
- **LEG-093** Outbox polling by the author (write-before-ack, idempotency).
  - **Accept**: author polls outbox, ack consumes; re-read after ack is empty;
    duplicate work-item (same id) is not executed twice.
- **LEG-094** Multi-node example (3 symmetric nodes, domain-free).
  - **Accept**: A triggers a 2-level flow that delegates a capability to B (or
    C) and receives the final result, all with the federation token;
    symmetric: B can trigger A the same way.

### R-10 — Hardening & release

- **LEG-100** Docs & examples hardening; glossary; consumer guide.
  - **Accept**: consumer guide (adding tools + patterns to a node) is
    executable top-to-bottom; examples are all green tests.
- **LEG-101** semver `0.1` + packaging + changelog + tags.
  - **Accept**: `uv build` produces a wheel/archive; `git tag v0.1.0` exists;
    changelog lists every merged issue.
- **LEG-102** An external consumer (own repo) pins the released `legio`, its
  validation suite green.
  - **Accept**: the consumer repo pins the released version; its validation
    suite runs green against it.

## Ordering constraints

- LEG-0xx before LEG-1xx..., except documentation (LEG-003) can be updated
  continuously.
- No implementation issue starts before its contract spec (the LEG-01x range
  covering it) is approved and its contract tests exist (red).
- No dependency is used before `docs/DEPENDENCIES.md` is approved.
- Per-rasante DoD requires the journal entry and the green validation case.