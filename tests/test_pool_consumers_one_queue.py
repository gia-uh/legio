"""GitHub #40 — strict n-consumers / 1-queue concurrency proof.

The ninth-audit gap: no strict concurrency test existed for the pooled consumer
model (ARCHITECTURE §4: ``pool_size`` replicas share **one** class queue and pop
one item once — no lease, no retry, no re-queue). This file proves the property
on exactly the real mounted path (LEG-087 §A): two live standing loops — the
instance replicas of a ``pool=2`` class, each spawned by its own parked
bring-up generator — race on the single class queue while M distinct business
requests are pending, and **each request is consumed exactly once**: no token
lost, none processed twice.

The observable is the tool's own invocation ledger. Every request carries a
unique token inside its payload (Schema 2), and the tool appends the token to a
shared list on every invocation. After the shared queue drains to empty:

  - ``len(invocations) == M``            (nothing processed twice)
  - ``set(invocations) == set(tokens)``  (nothing lost)

Both failure modes of a racy pop would break one of the two invariants: a
duplicate consume repeats a token (count > M / duplicate in set), a lost pop
gaps a token (missing in set).

The test never uses the lifecycle verbs (disable/enable/destroy): the
``pool > 1`` control-state caveat is documented debt (shared per-agent control
state), while **business** consumption — what this proof targets — is safe, and
teardown cancels the pumps, which closes each parked bring-up generator and
cancels its standing loop (the manager-level cancel path, LEG-087 §A).

Written red first against the #40 gap (contract-first).
"""

from __future__ import annotations

import asyncio
import time

import pytest
from beaver import AsyncBeaverDB

from legio.agents.tool_agent import ToolAgent
from legio.flow import ControlVerifier, ExecutionRequestMessage
from legio.naming import queue_key, result_queue_key
from legio.patterns import load_patterns
from legio.runtime import Runtime
from legio.tools import AvailableToolsRegistry

NODE_ID = "pool@host"
KEY = bytes(range(32))
TOKENS = 16
DRAIN_DEADLINE = 10.0

# The single observable of the proof: every tool invocation appends its token.
# The tool runs off the loop (asyncio.to_thread); list.append is atomic under
# the GIL, and the assertions only need the multiset, never an order.
_INVOCATIONS: list[str] = []


def record_token(text: str) -> dict[str, str]:
    """Domain-free tool: record the request's unique token in the ledger."""
    _INVOCATIONS.append(text)
    return {"text": text}


def _atomic_yaml(name: str) -> str:
    return f"""\
name: {name}
type: atomic
kind: tool
input:
  input_as: {name}
  input_type: json
  input_schema:
    type: object
    properties:
      text: {{type: string}}
output:
  output_as: {name}
  output_type: json
  output_schema:
    type: object
    properties:
      text: {{type: string}}
tool: {name}
parameters:
  text: "{{{name}.text}}"
"""


def _runtime(db: AsyncBeaverDB) -> Runtime:
    return Runtime(db, node_id=NODE_ID, control_key=KEY)


def _pool_agent(db: AsyncBeaverDB) -> ToolAgent:
    registry = AvailableToolsRegistry()
    registry.declare(
        "pooler",
        implementation="tests.test_pool_consumers_one_queue.record_token",
        policy={"timeout": 30, "retries": 0},
    )
    return ToolAgent(
        agent_id="pooler",
        db=db,
        available_tools=registry,
        tool_name="pooler",
        parameters={"text": "{pooler.text}"},
        control_verifier=ControlVerifier(KEY),
    )


def _start_executor(runtime: Runtime, *, pumps: int = 3) -> list[asyncio.Task]:
    """The node's executor pool (LEG-085 §A): each parked bring-up occupies one
    whole dispatch, so the node runs one executor per life-task and spares for
    facts — the tests start a pool the same way a booted node must."""

    async def _loop() -> None:
        while True:
            await runtime.manager.run()
            await asyncio.sleep(0)

    return [asyncio.create_task(_loop()) for _ in range(pumps)]


async def _cancel_pumps(pumps: list[asyncio.Task]) -> None:
    """Teardown: cancelling the pumps closes each parked bring-up generator,
    whose ``finally`` cancels its standing loop (LEG-087 §A manager-level
    cancel). No lifecycle verb is used — pool>1 control state is documented
    debt, and business consumption is what this test proves."""
    for pump in pumps:
        pump.cancel()
    if pumps:
        await asyncio.gather(*pumps, return_exceptions=True)


@pytest.mark.asyncio
async def test_pooled_consumers_share_one_queue_exactly_once(beaver_db) -> None:
    """Two replicas consume one shared class queue: each of M distinct requests
    is processed exactly once, under real concurrent standing loops."""
    _INVOCATIONS.clear()
    runtime = _runtime(beaver_db)
    runtime.mount_agents({"pooler": _pool_agent(beaver_db)})
    pumps = _start_executor(runtime)
    try:
        catalog = load_patterns(_atomic_yaml("pooler"))
        spec = catalog.specs["pooler"]
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("pooler"), pool=2)

        instances = await runtime.list_instances("pooler")
        assert len(instances) == 2, "pool=2 must materialize two replicas"

        tokens = [f"token-{i:03d}" for i in range(TOKENS)]
        shared_queue = beaver_db.queue(queue_key("pooler"))
        for token in tokens:
            request = ExecutionRequestMessage(
                level_route=(("pooler", "pooler"),),
                current_index=0,
                end_of_level_queue=result_queue_key("pooler"),
                level=1,
                launcher_class="pooler",
                task_id=f"{NODE_ID}:{token}",
                payload={"pooler": {"text": token}},
            )
            await shared_queue.put(request.model_dump(mode="json"), priority=0.0)

        # Both loops consume asynchronously (beaver wakes the blocked getters);
        # the bounded wait is the sanctioned rule-8 exception for tests.
        # Note: ``count()`` reaching 0 is necessary but not sufficient — an
        # atomic pop deletes the row *before* the tool runs (off-loop via
        # ``to_thread``), so the ledger may still be a few calls behind. Wait
        # for the queue *and* the invocation ledger to settle.
        deadline = time.monotonic() + DRAIN_DEADLINE
        while True:
            drained = await shared_queue.count() == 0
            settled = len(_INVOCATIONS) >= TOKENS
            if drained and settled:
                break
            assert time.monotonic() < deadline, (
                "pooled consumers stalled: queue drained but ledger unsettled"
            )
            await asyncio.sleep(0.01)

        assert sorted(_INVOCATIONS) == sorted(tokens), (
            "pooled consumers violated exactly-once: "
            f"{len(_INVOCATIONS)} invocations for {len(tokens)} tokens "
            f"(lost={sorted(set(tokens) - set(_INVOCATIONS))}, "
            f"dup={sorted({t for t in _INVOCATIONS if _INVOCATIONS.count(t) > 1})})"
        )
    finally:
        await _cancel_pumps(pumps)
