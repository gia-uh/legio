"""Tests for LEG-022 — ToolAgent execution path (Schema 1/2/3 over native beaver).

The ToolAgent pops a work item from its native beaver queue, reads the payload
from the message's single ``payload`` container (Schema 2), resolves the terse
``parameters`` (`{arg: dotted.path | literal}`) against it, loads the bound
``tool: <name>`` from ``available_tools`` (Schema 3), invokes it with the
resolved kwargs, validates the call against the tool's signature at execution
time, and builds the new payload with ``build_payload``. The base routes by
position: advance to the next class of the level, or deposit an
``ExecutionResultMessage`` to the level's ``end_of_level_queue``. The step's
state travels in the messages (AGENT_LIFECYCLE §12.1): nothing is staged
out-of-message. Schema/signature failures on either edge are never silent:
the failure is visible in the outcome. All state lives on native beaver
primitives.
"""

from __future__ import annotations

import pytest
from beaver import AsyncBeaverDB

from legio.agents.tool_agent import ToolAgent
from legio.flow import ExecutionRequestMessage, ExecutionResultMessage
from legio.naming import queue_key
from legio.tools import AvailableToolsRegistry


async def pop_one(db: AsyncBeaverDB, agent_id: str) -> dict | None:
    try:
        item = await db.queue(queue_key(agent_id)).get(block=False)
    except IndexError:
        return None
    return item.data


def crafted_request(
    *, task_id: str, payload: dict
) -> ExecutionRequestMessage:
    return ExecutionRequestMessage(
        level_route=(("main_a", "main_a"), ("summ", "summ")),
        current_index=1,
        end_of_level_queue="main_a",
        task_id=task_id,
        payload=payload,
    )


@pytest.mark.asyncio
async def test_fake_tool_executes_end_to_end_with_result_deposited(
    beaver_db: AsyncBeaverDB,
) -> None:
    registry = AvailableToolsRegistry()
    registry.declare(
        "transform",
        implementation="tests.test_tools.fake_transform",
        policy={"timeout": 30, "retries": 0},
    )

    request = crafted_request(task_id="T-1", payload={"summ": {"text": "hello", "factor": 2}})
    await beaver_db.queue(queue_key("summ")).put(request.model_dump(mode="json"), priority=0.0)

    agent = ToolAgent(
        agent_id="summ",
        db=beaver_db,
        available_tools=registry,
        tool_name="transform",
        parameters={"text": "{summ.text}", "factor": "{summ.factor}"},
        input_as="summ",
        output_as="summ",
    )

    handled = await agent.process_next()
    assert handled is True

    result_item = await pop_one(beaver_db, "main_a")
    assert result_item is not None
    result = ExecutionResultMessage.model_validate(result_item)
    assert result.task_id == "T-1"
    # factor=2, so "HELLO" * 2 = "HELLOHELLO", wrapped under the agent's output_as
    assert result.payload["summ"]["transformed"] == "HELLOHELLO"


@pytest.mark.asyncio
async def test_input_signature_rejection_is_never_silent(
    beaver_db: AsyncBeaverDB,
) -> None:
    registry = AvailableToolsRegistry()
    registry.declare(
        "transform",
        implementation="tests.test_tools.fake_transform",
        policy={"timeout": 30, "retries": 0},
    )

    # Missing required parameter 'text' (input read under the agent's input_as)
    request = crafted_request(task_id="T-bad-in", payload={"summ": {"factor": 2}})
    await beaver_db.queue(queue_key("summ")).put(request.model_dump(mode="json"), priority=0.0)

    agent = ToolAgent(
        agent_id="summ",
        db=beaver_db,
        available_tools=registry,
        tool_name="transform",
        parameters={"text": "{summ.text}", "factor": "{summ.factor}"},
        input_as="summ",
        output_as="summ",
    )

    await agent.process_next()

    result_item = await pop_one(beaver_db, "main_a")
    assert result_item is not None
    result = ExecutionResultMessage.model_validate(result_item)
    assert result.task_id == "T-bad-in"
    assert "error" in result.payload


@pytest.mark.asyncio
async def test_output_validation_failure_is_never_silent(beaver_db: AsyncBeaverDB) -> None:
    """Declared output_schema violation at the agent edge is a visible error.

    The tool returns ``{"transformed": "HELLOHELLO"}`` (a string); the pattern
    declares the output contract as **integer** — under the strict superset
    check the built value fails and the failure routes as an ``error`` result,
    never silent (AGENTS.md rule 9).
    """
    registry = AvailableToolsRegistry()
    registry.declare(
        "transform",
        implementation="tests.test_tools.fake_transform",
        policy={"timeout": 30, "retries": 0},
    )

    request = crafted_request(task_id="T-bad-out", payload={"summ": {"text": "hello", "factor": 2}})
    await beaver_db.queue(queue_key("summ")).put(request.model_dump(mode="json"), priority=0.0)

    agent = ToolAgent(
        agent_id="summ",
        db=beaver_db,
        available_tools=registry,
        tool_name="transform",
        parameters={"text": "{summ.text}", "factor": "{summ.factor}"},
        input_as="summ",
        output_as="summ",
        output_schema={
            "type": "object",
            "properties": {"transformed": {"type": "integer"}},
        },
    )

    await agent.process_next()

    result_item = await pop_one(beaver_db, "main_a")
    assert result_item is not None
    result = ExecutionResultMessage.model_validate(result_item)
    assert result.task_id == "T-bad-out"
    assert "error" in result.payload
    assert "output contract rejected" in result.payload["error"]


@pytest.mark.asyncio
async def test_input_contract_rejection_is_never_silent(beaver_db: AsyncBeaverDB) -> None:
    """Declared input_schema violation at the agent edge is a visible error.

    The incoming data fails the strict superset check (``factor`` is "two",
    not the declared ``integer``) before the tool is invoked — surfaced as an
    ``error`` result, never silent.
    """
    registry = AvailableToolsRegistry()
    registry.declare(
        "transform",
        implementation="tests.test_tools.fake_transform",
        policy={"timeout": 30, "retries": 0},
    )

    request = crafted_request(
        task_id="T-bad-in-schema", payload={"summ": {"text": "hello", "factor": "two"}}
    )
    await beaver_db.queue(queue_key("summ")).put(request.model_dump(mode="json"), priority=0.0)

    agent = ToolAgent(
        agent_id="summ",
        db=beaver_db,
        available_tools=registry,
        tool_name="transform",
        parameters={"text": "{summ.text}", "factor": "{summ.factor}"},
        input_as="summ",
        output_as="summ",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}, "factor": {"type": "integer"}},
        },
    )

    await agent.process_next()

    result_item = await pop_one(beaver_db, "main_a")
    assert result_item is not None
    result = ExecutionResultMessage.model_validate(result_item)
    assert result.task_id == "T-bad-in-schema"
    assert "error" in result.payload
    assert "input contract rejected" in result.payload["error"]


@pytest.mark.asyncio
async def test_contract_check_accepts_superset_extras(beaver_db: AsyncBeaverDB) -> None:
    """Superset semantics: declared properties present, extras pass through.

    The input carries an undeclared ``extra`` key and the output matches the
    declared schema — the check passes and the result is deposited normally.
    """
    registry = AvailableToolsRegistry()
    registry.declare(
        "transform",
        implementation="tests.test_tools.fake_transform",
        policy={"timeout": 30, "retries": 0},
    )

    request = crafted_request(
        task_id="T-super",
        payload={"summ": {"text": "hello", "factor": 2, "extra": "irrelevant"}},
    )
    await beaver_db.queue(queue_key("summ")).put(request.model_dump(mode="json"), priority=0.0)

    agent = ToolAgent(
        agent_id="summ",
        db=beaver_db,
        available_tools=registry,
        tool_name="transform",
        parameters={"text": "{summ.text}", "factor": "{summ.factor}"},
        input_as="summ",
        output_as="summ",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}, "factor": {"type": "integer"}},
        },
        output_schema={
            "type": "object",
            "properties": {"transformed": {"type": "string"}},
        },
    )

    await agent.process_next()

    result_item = await pop_one(beaver_db, "main_a")
    assert result_item is not None
    result = ExecutionResultMessage.model_validate(result_item)
    assert result.task_id == "T-super"
    assert result.payload["summ"]["transformed"] == "HELLOHELLO"


@pytest.mark.asyncio
async def test_no_due_item_returns_false(beaver_db: AsyncBeaverDB) -> None:
    registry = AvailableToolsRegistry()
    registry.declare(
        "transform",
        implementation="tests.test_tools.fake_transform",
        policy={"timeout": 30, "retries": 0},
    )

    agent = ToolAgent(
        agent_id="summ",
        db=beaver_db,
        available_tools=registry,
        tool_name="transform",
        parameters={"text": "{summ.text}", "factor": "{summ.factor}"},
        input_as="summ",
        output_as="summ",
    )

    assert await agent.process_next() is False