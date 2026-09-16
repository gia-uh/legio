# DEPENDENCIES — Report for approval

Status: **APPROVED** (decision of 2026-08-27). No dependency may be added or
removed before a reviewed PR to this file. Managed with `uv`. Python runtime:
**>= 3.13** (matching beaver's requirement).

## Runtime

| Package | Role in legio | Notes |
|---|---|---|
| `beaver-db` | registries, priority queues, locks (the substrate) | pinned by `uv.lock` at first install |
| `lingo-ai` | LLM + structured output for linguistic agents | provides `LLM`, `eng.create/decide/choose`, `MockLLM`, native tool-calling |
| `pydantic` (>=2) | types/validation of message, token, patterns, schemas | lingua franca of the system |
| `pydantic-settings` | configuration from env | node id, paths, peers |
| `pyyaml` | parse patterns YAML | |
| `fastapi` | API + federation server endpoints | |
| `uvicorn` | ASGI server for the above | |
| `httpx` | HTTP client for federation + tool calls | async |
| `typer` | Runtime CLI (`legio server` / `legio agent`, LEG-081) | approved 2026-09-15 (session 86h); already pinned in `uv.lock` via `beaver-db`, promoted to a direct runtime dependency |

## Dev

| Package | Role |
|---|---|
| `ruff` | lint + format |
| `pytest` | test runner |
| `pytest-asyncio` | async tests |
| `respx` | mock `httpx` (tools, inter-node HTTP) |
| `pyright` (optional) | type checking (optional; decide at R-0) |

## Excluded on purpose

- `castor-io`: outdated against current beaver, **never used**; legio implements
  its own generic task environment (the **Manager**) on beaver (see
  `docs/AGENT_LIFECYCLE.md` §6.1).
- **Task-queue / task-executor libraries evaluated and rejected** (criteria:
  minimal, no Redis/broker, isolated dependencies, and — critically — backed by
  **beaver** as the substrate; note these are generic task-queue libraries, not
  a legio concept):
  - `rq`, `celery`, `dramatiq`, `arq`, `taskiq`(redis): require Redis/broker.
    Rejected.
  - `procrastinate`: requires a PostgreSQL server — remote infra, not embedded.
    Rejected.
  - `huey`, `apscheduler`, `schedule`: embedded (sqlite / in-memory), but backed
    by their **own** persistence, not by beaver — adopting one would introduce a
    **second substrate** alongside beaver (duplicating persistence). Rejected.
  - Conclusion: no pure-Python library both fulfills the criteria **and** sits on
    beaver. legio implements its own `Manager` on beaver (single substrate,
    no Redis, no extra dependencies).
- Any broker, Redis, DB server, workflow engine, scheduler library, or
  callback/event library: all covered by beaver + the design's polling/field
  semantics.

Approve, add or remove items. Changes only via reviewed PR to this file.