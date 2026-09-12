"""Integration test for the decoupled polling flow over native beaver.

Pins the corrected, decoupled model over the real modules:

1. ``Runtime.submit`` (mounted on ``Manager.submit_task``, §7.1) stages the
   task; its ``seed`` fact — dispatched by the node pump (``manager.run()``) —
   deposits the first ``ExecutionRequestMessage`` (root step, Schema 2 token
   with ``end_of_level_queue`` = the task's final-result queue) into the
   starting agent's queue (``db.queue("legio:queue:transform")``).
2. A ``ToolAgent`` runs its own loop and *polls* that queue — it never knows the
   client or the task, only the queue.
3. The agent routes by position and, closing level 1, writes the result to
   ``end_of_level_queue`` (the task's final-result queue).
4. ``Runtime.status`` (peeking that queue) reflects COMPLETED with the output.

This validates the critical corrections so that LEG-025/026 are built on a
faithful decoupled base, not an orchestrated one. No invented substrate layer;
there is no ``results`` store.
"""

from __future__ import annotations

import pytest
from beaver import AsyncBeaverDB

from legio.agents.tool_agent import ToolAgent
from legio.flow import ExecutionResultMessage
from legio.naming import queue_key, result_queue_key
from legio.runtime import Runtime
from legio.tools import AvailableToolsRegistry


def fake_transform(text: str) -> dict:
    """Domain-free fake tool: plain callable, signature is its contract."""
    return {"transformed": str(text).upper()}


def build_transform_agent(db: AsyncBeaverDB) -> ToolAgent:
    registry = AvailableToolsRegistry()
    registry.declare(
        "transform",
        implementation="tests.test_tools.fake_transform",
        policy={"timeout": 30, "retries": 0},
    )
    return ToolAgent(
        agent_id="transform",
        db=db,
        available_tools=registry,
        tool_name="transform",
        parameters={"text": "{transform.text}"},
        input_as="transform",
        output_as="transform",
    )


@pytest.mark.asyncio
async def test_submit_deposits_step_one_in_starting_agent_queue(
    beaver_db: AsyncBeaverDB,
    runtime: Runtime,
) -> None:
    task_id = await runtime.submit(
        "client-a", (("transform", "transform"),), {"text": "hello"}
    )
    # The seed task deposits the root message on the node pump (§7.1).
    await runtime.manager.run()

    item = await beaver_db.queue(queue_key("transform")).get(block=False)
    assert item.data["task_id"] == task_id
    assert item.data["current_index"] == 0
    assert item.data["level"] == 1
    assert item.data["level_route"] == [["transform", "transform"]]
    assert item.data["end_of_level_queue"] == result_queue_key(task_id)
    assert item.data["payload"] == {"transform": {"text": "hello"}}


@pytest.mark.asyncio
async def test_decoupled_root_flow_writes_result_queue_and_status_completed(
    beaver_db: AsyncBeaverDB,
    runtime: Runtime,
) -> None:
    task_id = await runtime.submit(
        "client-a", (("transform", "transform"),), {"text": "hello"}
    )
    # The seed task deposits the root message on the node pump (§7.1).
    await runtime.manager.run()

    agent = build_transform_agent(beaver_db)
    steps = await agent.run()
    assert steps == 1

    entry = await runtime.status(task_id, "client-a")
    assert entry.state.value == "completed"
    assert entry.output == {"transform": {"transformed": "HELLOHELLO"}}
    assert entry.result_key == result_queue_key(task_id)

    result_item = await beaver_db.queue(queue_key(result_queue_key(task_id))).peek()
    assert result_item is not None
    result = ExecutionResultMessage.model_validate(result_item.data)
    assert result.payload == {"transform": {"transformed": "HELLOHELLO"}}