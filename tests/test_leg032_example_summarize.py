"""Contract tests for LEG-032 — Example: ``summarize`` (linguistic → tool).

The first R-3 domain-free example: a ``summarize`` flow composed of two
*independent, standing* atomic agents — a ``summ`` linguistic agent (driven by a
``MockLLM`` fake) that produces structured output, and an ``assess`` tool agent
that consumes that structured output and produces the final result — driven by a
unified ``CompositeAgent`` (single branch = a sequence, LEG-040/LEG-044). They run
through the decoupled polling model (AGENTS.md/ARCH): each agent is booted at
node startup with its own native beaver queue, polls its own queue, and routes
by the token — there is no central engine and nothing is loaded dynamically at
submit time. All substrate is native beaver.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from beaver import AsyncBeaverDB
from lingo.mock import MockLLM
from pydantic import BaseModel

from legio.agents.composite_agent import CompositeAgent
from legio.agents.linguistic_agent import LinguisticAgent
from legio.agents.tool_agent import ToolAgent
from legio.api import create_app
from legio.flow import build_payload
from legio.naming import outbox_key
from legio.patterns import resolve_composite_branches
from legio.runtime import Runtime
from legio.security import ClientTokenStore
from legio.tools import AvailableToolsRegistry

# The patterns come from the shared example-node single source (LEG-100):
# ``examples/summarize/patterns/{linguistic,tool,composite}`` — the same files
# the consumer guide documents. Any drift there breaks this test (no bitrot).
from tests.conftest import load_example_node


class SummarizeOutput(BaseModel):
    title: str
    summary: str
    word_count: int = 0


class AssessOutput(BaseModel):
    result: str


class GatherComposite(CompositeAgent):
    """A concrete composite pattern (fictitious domain): transcribes each
    branch's built payload (slot order) into its own ``output_as``.

    The engine's ``CompositeAgent`` has **no** generic default build — output
    construction is the pattern's model (it must satisfy the pattern's declared
    ``output_schema``); each concrete composite inherits and implements it.
    """

    async def build_output_as(self, info):
        gathered: dict = {}
        for payload in info.values():
            gathered.update(payload)
        return build_payload(gathered, output_as=self._output_as)


def fake_assess(title: str, summary: str) -> dict:
    """Domain-free fake tool: plain callable, signature is its contract."""
    return {"result": f"[{title}] {summary}"}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def build_standing_agents(
    db: AsyncBeaverDB,
) -> tuple[CompositeAgent, LinguisticAgent, ToolAgent]:
    """Boot the composite and its two independent standing agents at node
    startup, each with its own queue."""
    lingo_client = MockLLM(
        responses=[SummarizeOutput(title="Foxes", summary="A note about foxes.", word_count=4)]
    )
    registry = AvailableToolsRegistry()
    registry.declare(
        "assess",
        implementation="tests.test_leg032_example_summarize.fake_assess",
        policy={"timeout": 30, "retries": 0},
    )

    catalog = load_example_node("summarize")
    summ_spec = catalog.specs["summ"]
    assess_spec = catalog.specs["assess"]
    composite_spec = catalog.specs["summarize"]

    summ = LinguisticAgent(
        agent_id="summ",
        db=db,
        lingo_client=lingo_client,
        prompt_template="Summarize {text} and {lang}.",
        output_model=SummarizeOutput,
        input_as=summ_spec.input.input_as,
        output_as=summ_spec.output.output_as,
        input_schema=summ_spec.input.input_schema,
        output_schema=summ_spec.output.output_schema,
    )
    assess = ToolAgent(
        agent_id="assess",
        db=db,
        available_tools=registry,
        tool_name="assess",
        parameters={"title": "{summ.title}", "summary": "{summ.summary}"},
        input_as=assess_spec.input.input_as,
        output_as=assess_spec.output.output_as,
        input_schema=assess_spec.input.input_schema,
        output_schema=assess_spec.output.output_schema,
    )

    branches = resolve_composite_branches(composite_spec, catalog)
    composite = GatherComposite(
        agent_id="summarize",
        db=db,
        branches=branches,
        input_as=composite_spec.input.input_as,
        output_as=composite_spec.output.output_as,
        input_schema=composite_spec.input.input_schema,
        output_schema=composite_spec.output.output_schema,
    )
    return composite, summ, assess


@pytest.mark.asyncio
async def test_summarize_flows_linguistic_to_tool_over_rest_and_auth(
    caplog: pytest.LogCaptureFixture, beaver_db: AsyncBeaverDB, runtime: Runtime
) -> None:
    caplog.set_level(logging.INFO)

    # Boot the standing agents
    composite, summ, assess = build_standing_agents(beaver_db)

    # Load the pattern catalog (the example-node single source)
    pattern_catalog = load_example_node("summarize")

    # Create the authenticated app with pattern catalog
    store = ClientTokenStore()
    store.register("client-a", token="tok-a")
    app = create_app(runtime=runtime, clients=store, pattern_catalog=pattern_catalog)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        # Submit to the summarize pattern
        resp = await ac.post(
            "/submit",
            json={
                "client_id": "client-a",
                "agent": "summarize",
                "payload": {"text": "The quick brown fox.", "lang": "en"},
            },
            headers=bearer("tok-a"),
        )
        assert resp.status_code == 200, resp.text
        task_id = resp.json()["task_id"]
        # The seed task deposits the root message on the node pump (§7.1).
        await runtime.manager.run()

        # Drive the composite fan-out, the branch steps, and the fan-in to
        # completion (interleaved, polling only).
        for _ in range(4):
            await composite.run()
            await summ.run()
            await assess.run()
            await composite.run()

        # Check status: the result sits on the agent's shared queue; status
        # schedules its collection (kick-on-miss), the pump dispatches it.
        await ac.get(f"/status/{task_id}", headers=bearer("tok-a"))
        for _ in range(10):
            await runtime.manager.run()
        status_resp = await ac.get(f"/status/{task_id}", headers=bearer("tok-a"))
        assert status_resp.status_code == 200, status_resp.text
        entry = status_resp.json()
        assert entry["state"] == "completed"
        assert entry["result_key"] == outbox_key(task_id)

        # The tool should have received the linguistic output (re-keyed under its
        # input_as and wrapped under the final output_as in the result)
        assert entry["output"]["result"]["result"]["result"] == "[Foxes] A note about foxes."

        log_text = caplog.text
        assert "manager submit" in log_text
        assert "agent run" in log_text
        assert "agent finish" in log_text
