# legio

A queue-based agentic orchestration engine. Independent library, **domain-free**:
all domain knowledge lives in patterns (YAML as data) and a tool registry
provided by the consumer. The library never knows about any specific consumer;
validation happens through the in-repo examples (`examples/`) and external
consumer repositories kept separate (AGENTS.md rule 7).

## Quick start

```bash
uv run legio server --config examples/transform/legio.yaml --host 127.0.0.1 --port 8000
```

Then submit and poll status (see `docs/CONSUMER_GUIDE.md` for the full
walkthrough; the headless `transform` example boots with no LLM needed).

## Docs surface

- `docs/CONSUMER_GUIDE.md` — executable walkthrough: register a tool → write a
  pattern → configure → boot → submit → status (validated by CI).
- `docs/GLOSSARY.md` — canonical definitions for the identifiers and
  architecture terms.
- `docs/ARCHITECTURE.md` — the architecture (read before any work).
- `docs/AGENT_LIFECYCLE.md` — the class/instance lifecycle (§4.8) and the three
  schemas (§4.11).
- `docs/CONTRIBUTING.md` — methodology, development flow, review checklist.
- `docs/PLAN.md` — the plan and issue roadmap; `docs/CONTRACTS/` holds the
  per-issue approved specs.
- `docs/DEPENDENCIES.md` — the approved dependency list.
- `docs/JOURNALS/` — turn-by-turn journaling; read the latest before working.

## Examples

`examples/` ships four self-contained, domain-free example nodes — each with
its own `patterns/` (Schema 1 YAML), `tools.yaml` (Schema 3) and `legio.yaml`
(LEG-017): `transform`, `summarize`, `extract-and-summarize` and
`distribute-summary`. The same files are exercised by the test suite — drift
breaks the build (LEG-100, no bitrot).

## Engine in one breath

The public API never pushes (polling only, `next_run_at` scheduling): the host
drives `runtime.manager.run()` one pass per dispatch; each standing agent polls
its own beaver queue; the immutable Schema 2 `FlowToken` (`level_route`,
`current_index`, `end_of_level_queue`, `level`, `launcher_class`,
`task_id`, `branch_id`, `message_type`, `payload`) travels the routes; the
final result lands on the agent's shared final-result queue and is collected
into the task's outbox record that `/status` reads. Errors are typed
(`legio.errors`) and never silent (rule 9); every module logs structured
`key=value` events under the `legio.*` tree (rule 11).

## Core capabilities (current)

- **Schemas**: S1 one-spec-per-pattern YAML with mandatory symmetric contracts;
  S2 the route token; S3 `available_tools` (`implementation` + `policy`).
- **Standing agents**: atomic `tool` and `linguistic` agents materialized at
  boot, and unified `composite` agents whose branches reference other agents by
  name (the composite's output build is the pattern's model, injected as a
  concrete class); nothing is loaded dynamically at submit time.
- **Dynamic lifecycle**: class/instance verbs (create/enable/disable/destroy),
  pools as capacity intent, bring-up leaves-first over the served catalog.
- **Runtime surface**: REST submit/status plus class/instance verb classes over
  beaver queues with a client token store; `legio server` and `legio agent
  <verb>` CLI.
- **Federation**: per-node catalogs, roster-based step routing over a beaver
  routing proxy — peers never widen scope (rule 9).

## State

R-0..R-9 core is **shipped** on the three-schema, decoupled polling engine
(exact per-issue status lives in `docs/CONTRACTS/` and `docs/JOURNALS/`).
R-10 (Hardening & release) is the current track: the `LEG-103`
audit-hardening series (contract-first slices, all green) and this session's
`LEG-100` docs & examples hardening (consumer guide + glossary + example tree)
await maintainer review; `LEG-101` (semver, packaging, changelog, tags) and
`LEG-102` come next on the release track.

## Development

`make ci` mirrors the CI gate exactly: lint (`ruff check`) + format check
(`ruff format --check`) + typecheck (`pyright`) + full `pytest`. Convenience
targets: `make sync`, `make lint`, `make format`, `make format-check`,
`make typecheck`, `make test`, `make build` (LEG-101 wheel/archive), `make
clean`, `make tag`/`make release` (maintainer only). Two known gates currently
show the documented baseline debt — `ruff format --check` (56 files, triage
from Session 108) and the 2 pyright false positives in
`tests/test_leg103_slice13_hardening.py`; both are tracked in `docs/JOURNALS/`
and kept separate from per-issue work. Everything in this repo is English
(AGENTS.md rule 1); work is per-issue, contract-first, and every turn ends
with a journal commit.