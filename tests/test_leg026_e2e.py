"""LEG-026 — End-to-end example: ``transform`` with a fake, domain-free tool.

First real single-node capability over the REST surface. A client submits to the
``transform`` agent through the API; the agent polls its own queue and runs the
registered fake tool; the root result lands in the task's final-result queue
(``end_of_level_queue``, Schema 2) and the client reads it back via ``status``.
Boundary is the REST surface; all substrate is shared in-process over a single
native beaver database (one connection, one manager) as on a single node.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from beaver import AsyncBeaverDB

from legio.agents.tool_agent import ToolAgent
from legio.api import create_app
from legio.naming import queue_key, result_queue_key
from legio.runtime import Runtime
from legio.tools import AvailableToolsRegistry


def fake_transform(text: str, factor: int = 2) -> dict:
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
        parameters={"text": "{transform.text}", "factor": "{transform.factor}"},
        input_as="transform",
        output_as="transform",
    )


@pytest.mark.asyncio
async def test_transform_e2e_over_rest_and_agent(
    caplog: pytest.LogCaptureFixture, beaver_db: AsyncBeaverDB, runtime: Runtime
) -> None:
    caplog.set_level(logging.INFO)
    app = create_app(runtime=runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/submit",
            json={
                "client_id": "client-a",
                "agent": "transform",
                "payload": {"text": "hello", "factor": 3},
            },
        )
        assert resp.status_code == 200, resp.text
        task_id = resp.json()["task_id"]
        # The seed task deposits the root message on the node pump (§7.1).
        await runtime.manager.run()

        agent = build_transform_agent(beaver_db)
        processed = await agent.run()
        assert processed == 1

        st = await ac.get(f"/status/{task_id}", params={"client_id": "client-a"})
        assert st.status_code == 200, st.text
        entry = st.json()
        assert entry["state"] == "completed"
        # factor=3, so "HELLO" * 3 = "HELLOHELLOHELLO", wrapped under output_as
        assert entry["output"] == {"transform": {"transformed": "HELLOHELLOHELLO"}}
        assert entry["result_key"] == result_queue_key(task_id)

        result_item = await beaver_db.queue(
            queue_key(result_queue_key(task_id))
        ).peek()
        assert result_item is not None
        assert result_item.data["payload"] == {
            "transform": {"transformed": "HELLOHELLOHELLO"},
        }

        log_text = caplog.text
        assert "manager submit" in log_text
        assert "agent run" in log_text
        assert "agent finish" in log_text