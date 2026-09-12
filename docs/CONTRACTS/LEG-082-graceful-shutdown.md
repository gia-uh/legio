# LEG-082 — Graceful shutdown + concurrency semaphores

- **Status:** DRAFT (awaiting maintainer approval)
- **Rasante:** R-8
- **GitHub issue:** #37
- **Source:** `docs/PLAN.md` (LEG-082), `docs/AGENT_LIFECYCLE.md` §12.2
- **Depends on:** LEG-013 (per-tool declaration), `services.llm` (LEG-017/LEG-030)

## Goal

Operational hygiene, engine-side, domain-free: per-tool and per-LLM
concurrency semaphores are honored under load, and a drain request (SIGTERM/
SIGINT wired by the deployment) makes every vehicle stop pulling new items
**between dispatches** while in-flight steps finish and always deposit their
result (§12.2: control is checked between dispatches — never inside a step,
never by parking or task-ifying the loop).

## Design anchors (verified, not improvised)

- §12.2 (`docs/AGENT_LIFECYCLE.md:1393-1398`): operational control is checked
  **between dispatches, never inside a step**; the loop stays the agent's own
  vehicle. The drain gate is exactly that: a flag the vehicle reads when deciding
  whether to pull the next item. No lease, no parking, no scheduling field, no
  new layer — the gate is a plain `asyncio.Event` the node's operator sets.
- Rule 8: the engine never sleeps and never polls a field. The semaphore
  acquire is a **cooperative wait** — the second documented rule-8 exception
  (the first being the bounded clock waits of §5.8) — never a `sleep`.
- Rule 7: no consumer domain (caps are generic limits, configured via the
  existing Schema 3 `policy` and `services.llm`).
- The Runtime does **not** pump, the Manager never owns a loop: drain is the
  vehicles' cooperative behavior plus the deployment stopping its
  `manager.run()` drives. Nothing is added to `legio.runtime`/`legio.manager`.

## Scope

- **In scope:**
  - `legio.concurrency` (new): `ConcurrencyCaps` (per-tool + per-LLM
    `asyncio.Semaphore`, lazy, shared across replicas, wait-not-fail),
    `ShutdownGate` (drain flag), `install_signal_drain` (deployment glue
    wiring SIGTERM/SIGINT → `gate.request()` on the running loop).
  - `AgentBase` (and `CompositeAgent`'s overriding `process_next`): honor an
    optional `ShutdownGate` between dispatches — once draining, no new item is
    pulled; in-flight steps keep running and always deposit (§12.2).
  - `ToolAgent`/`LinguisticAgent`: optional `ConcurrencyCaps`; acquire the
    per-tool / per-LLM semaphore around the resource call (execution bound,
    never before popping the item).
  - Config: `ToolPolicy.concurrency` (`>= 1` when set), `LlmConfig.
    max_concurrency` (`>= 1` when set); a 0/negative cap is a loud
    `ConfigError` (rule 9).
  - `materializer`: `materialize_agents`/`boot_node` build/accept a
    `ConcurrencyCaps` from `policy.concurrency` + `services.llm.
    max_concurrency` and thread it and the optional `ShutdownGate` into the
    materialized agents (seams, like `lingo_factory`).
- **Out of scope:** load-balancing / pool coordination (LEG-080); any pump,
  supervisor or central scheduler; the CLI's own SIGTERM wiring (LEG-081, last —
  it will use `install_signal_drain`); anything that sleeps or re-queues.

## Interface

- `legio.concurrency.ConcurrencyCaps(*, max_llm: int | None = None,
  tool_limits: Mapping[str, int] | None = None)`
  - `tool(name) -> AsyncIterator[None]` (async context manager): wait on the
    tool's semaphore, or no-op when no cap is declared.
  - `llm() -> AsyncIterator[None]`: wait on the node-wide LLM semaphore, or
    no-op when `max_llm` is unset.
  - Invalid caps (`< 1`) → `ValueError` at construction (rule 9).
- `legio.concurrency.ShutdownGate`
  - `draining: bool`, `request()` (idempotent, INFO-logged).
- `legio.concurrency.install_signal_drain(gate, *, loop=None,
  signals=(SIGTERM, SIGINT))` — `loop.add_signal_handler(sig, gate.request)`.
- Agent constructors gain two optional, keyword-only params:
  `concurrency: ConcurrencyCaps | None = None`, `drain: ShutdownGate | None =
  None` (default `None` = current behavior, no regression).

## Acceptance criteria

From `docs/PLAN.md` (LEG-082), verbatim:
- SIGTERM drains in-flight work before exit; per-tool concurrency cap is
  honored under load (test with a slow fake tool).
  Covered by: in-process real-SIGTERM drain test (slow fake tool); per-tool
  cap test (two replicas, slow tool, peak concurrency == cap); per-LLM cap
  test; default-unbounded regression test.

## Tests (contract, red first)

`tests/test_leg082_concurrency.py`:
- per-tool cap honored under load (2 replicas, slow sync tool, peak == 1);
- per-LLM cap honored under load (2 replicas, blocking fake lingo, peak == 1);
- no caps → unchanged/unbounded peak == replicas;
- `ShutdownGate.request()` mid-in-flight: result deposited, no new item pulled;
- real SIGTERM → gate set → drain (skipped when the runner has no signal
  handlers);
- invalid caps (`0`) → `ValueError`;
- config: `policy.concurrency: 0` / `services.llm.max_concurrency: 0` →
  `ConfigError`; valid positive values load;
- materializer wiring (behavioral): cap declared in tools config / `services.
  llm` is honored through the `boot_node`-built agents (peak == cap).

## Validation case

Slow-fake-tool load test (LEG-080 lane): N replicas on one class queue, cap K
→ peak concurrent tool executions never exceeds K; constrained tool waits,
never fails or drops.

## Definition of done

- All acceptance criteria met by running checks.
- Module logging is in place at every observable lifecycle point (rule 11):
  drain requested, drain holding dispatch, concurrency waits (debug).
- No regressions: full suite + ruff + pyright green.
- No-acoupling audit: adopters pass, no new beaver scope, no Runtime/Manager
  change, no import edge crossing layers, no consumer-domain names.
- Maintainer approval recorded; the maintainer closes the GitHub issue.
- Journal entry appended.