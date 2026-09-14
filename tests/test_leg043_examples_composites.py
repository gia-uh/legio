"""Contract tests for LEG-043 — Composite examples (R-4/LEG-044, domain-free).

Two in-repo examples that prove the R-4 engine end-to-end over REST
(LEG-025/LEG-027), as a client would drive them:

- ``extract_and_summarize`` — a composite with **one branch** (a sequence),
  linguistic → tool.
- ``distribute_summary`` — a composite with **two branches** (a parallel root),
  two linguistic branches joined on all-complete.

Both run on the **same unified CompositeAgent runner** (LEG-040/LEG-044): the
submit delivers to the composite's own class (a dumb delivery point — it never
resolves the DAG), the CompositeAgent fans out its branches and joins them.
No central engine; nothing is loaded dynamically at submit time.
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
from legio.patterns import load_patterns, resolve_composite_branches
from legio.runtime import Runtime
from legio.security import ClientTokenStore
from legio.tools import AvailableToolsRegistry

FULL_EXTRACT_YAML = """
name: extract
type: atomic
kind: linguistic
input:
  input_as: payload
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
      lang: {type: string}
output:
  output_as: extract
  output_type: json
  output_schema:
    type: object
    properties:
      title: {type: string}
      summary: {type: string}
      word_count: {type: integer}
prompt: "Extract the key points of {text} in {lang}."
---
name: assess
type: atomic
kind: tool
input:
  input_as: extract
  input_type: json
  input_schema:
    type: object
    properties:
      title: {type: string}
      summary: {type: string}
output:
  output_as: result
  output_type: json
  output_schema:
    type: object
    properties:
      result: {type: string}
tool: assess
parameters:
  title: "{extract.title}"
  summary: "{extract.summary}"
"""

EXTRACT_AND_SUMMARIZE_YAML = (
    FULL_EXTRACT_YAML
    + """
---
name: extract_and_summarize
type: composite
main: true
input:
  input_as: payload
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
      lang: {type: string}
output:
  output_as: result
  output_type: json
  output_schema:
    type: object
    properties:
      result:
        type: object
        properties:
          result: {type: string}
branches:
  - - extract
    - assess
"""
)

DISTRIBUTE_SUMMARY_YAML = """
name: summ
type: atomic
kind: linguistic
input:
  input_as: text
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
output:
  output_as: summ
  output_type: json
  output_schema:
    type: object
    properties:
      summ: {type: string}
prompt: "Summarize: {text}"
---
name: cata
type: atomic
kind: linguistic
input:
  input_as: text
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
output:
  output_as: cata
  output_type: json
  output_schema:
    type: object
    properties:
      cata: {type: string}
prompt: "Categorize: {text}"
---
name: distribute_summary
type: composite
main: true
input:
  input_as: payload
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
output:
  output_as: result
  output_type: json
  output_schema:
    type: object
    properties:
      summ:
        type: object
        properties:
          summ: {type: string}
      cata:
        type: object
        properties:
          cata: {type: string}
branches:
  - - summ
  - - cata
"""


class ExtractOutput(BaseModel):
    title: str
    summary: str
    word_count: int = 0


class SummOutput(BaseModel):
    summ: str = ""


class CataOutput(BaseModel):
    cata: str = ""


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


def build_tool_registry() -> AvailableToolsRegistry:
    registry = AvailableToolsRegistry()
    registry.declare(
        "assess",
        implementation="tests.test_leg043_examples_composites.fake_assess",
        policy={"timeout": 30, "retries": 0},
    )
    return registry


def build_single_branch_agents(
    db: AsyncBeaverDB,
) -> tuple[CompositeAgent, LinguisticAgent, ToolAgent]:
    """Boot the unified composite (single branch) + its standing atomic agents."""
    lingo_client = MockLLM(
        responses=[ExtractOutput(title="Foxes", summary="A note about foxes.", word_count=4)]
    )
    registry = build_tool_registry()

    catalog = load_patterns(EXTRACT_AND_SUMMARIZE_YAML)
    extract_spec = catalog.specs["extract"]
    assess_spec = catalog.specs["assess"]
    composite_spec = catalog.specs["extract_and_summarize"]

    extract = LinguisticAgent(
        agent_id="extract",
        db=db,
        lingo_client=lingo_client,
        prompt_template="Extract the key points of {text} in {lang}.",
        output_model=ExtractOutput,
        input_as=extract_spec.input.input_as,
        output_as=extract_spec.output.output_as,
        input_schema=extract_spec.input.input_schema,
        output_schema=extract_spec.output.output_schema,
    )
    assess = ToolAgent(
        agent_id="assess",
        db=db,
        available_tools=registry,
        tool_name="assess",
        parameters={"title": "{extract.title}", "summary": "{extract.summary}"},
        input_as=assess_spec.input.input_as,
        output_as=assess_spec.output.output_as,
        input_schema=assess_spec.input.input_schema,
        output_schema=assess_spec.output.output_schema,
    )

    branches = resolve_composite_branches(composite_spec, catalog)
    comp = GatherComposite(
        agent_id="extract_and_summarize",
        db=db,
        branches=branches,
        input_as=composite_spec.input.input_as,
        output_as=composite_spec.output.output_as,
        input_schema=composite_spec.input.input_schema,
        output_schema=composite_spec.output.output_schema,
    )
    return comp, extract, assess


def build_multi_branch_agents(
    db: AsyncBeaverDB,
) -> tuple[CompositeAgent, LinguisticAgent, LinguisticAgent]:
    """Boot the unified composite (two branches) + its standing atomic agents."""
    catalog = load_patterns(DISTRIBUTE_SUMMARY_YAML)
    summ_spec = catalog.specs["summ"]
    cata_spec = catalog.specs["cata"]
    composite_spec = catalog.specs["distribute_summary"]
    summ = LinguisticAgent(
        agent_id="summ",
        db=db,
        lingo_client=MockLLM(responses=[SummOutput(summ="a summary")]),
        prompt_template="Summarize: {text}",
        output_model=SummOutput,
        input_as=summ_spec.input.input_as,
        output_as=summ_spec.output.output_as,
        input_schema=summ_spec.input.input_schema,
        output_schema=summ_spec.output.output_schema,
    )
    cata = LinguisticAgent(
        agent_id="cata",
        db=db,
        lingo_client=MockLLM(responses=[CataOutput(cata="a category")]),
        prompt_template="Categorize: {text}",
        output_model=CataOutput,
        input_as=cata_spec.input.input_as,
        output_as=cata_spec.output.output_as,
        input_schema=cata_spec.input.input_schema,
        output_schema=cata_spec.output.output_schema,
    )

    branches = resolve_composite_branches(composite_spec, catalog)
    comp = GatherComposite(
        agent_id="distribute_summary",
        db=db,
        branches=branches,
        input_as=composite_spec.input.input_as,
        output_as=composite_spec.output.output_as,
        input_schema=composite_spec.input.input_schema,
        output_schema=composite_spec.output.output_schema,
    )
    return comp, summ, cata


@pytest.mark.asyncio
async def test_extract_and_summarize_single_branch_over_rest(
    caplog: pytest.LogCaptureFixture, beaver_db: AsyncBeaverDB, runtime: Runtime
) -> None:
    caplog.set_level(logging.INFO)
    comp, extract, assess = build_single_branch_agents(beaver_db)
    pattern_catalog = load_patterns(EXTRACT_AND_SUMMARIZE_YAML)

    store = ClientTokenStore()
    store.register("client-a", token="tok-a")
    app = create_app(runtime=runtime, clients=store, pattern_catalog=pattern_catalog)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/submit",
            json={
                "client_id": "client-a",
                "agent": "extract_and_summarize",
                "payload": {"text": "The quick brown fox.", "lang": "en"},
            },
            headers=bearer("tok-a"),
        )
        assert resp.status_code == 200, resp.text
        task_id = resp.json()["task_id"]
        # The seed task deposits the root message on the node pump (§7.1).
        await runtime.manager.run()

        # The composite fans out its single branch; run the branch steps and
        # the composite's fan-in repeatedly to a standstill.
        for _ in range(4):
            await comp.run()
            await extract.run()
            await assess.run()
            await comp.run()

        status_resp = await ac.get(f"/status/{task_id}", headers=bearer("tok-a"))
        for _ in range(10):
            await runtime.manager.run()
        status_resp = await ac.get(f"/status/{task_id}", headers=bearer("tok-a"))
        assert status_resp.status_code == 200, status_resp.text
        entry = status_resp.json()
        assert entry["state"] == "completed"
        assert entry["result_key"] == outbox_key(task_id)
        # single branch: assess produced {"result": "..."} under its output_as,
        # the composite gathered it under its own output_as "result"
        assert entry["output"]["result"]["result"]["result"] == "[Foxes] A note about foxes."


@pytest.mark.asyncio
async def test_distribute_summary_multi_branch_root_over_rest(
    caplog: pytest.LogCaptureFixture, beaver_db: AsyncBeaverDB, runtime: Runtime
) -> None:
    caplog.set_level(logging.INFO)
    comp, summ, cata = build_multi_branch_agents(beaver_db)
    pattern_catalog = load_patterns(DISTRIBUTE_SUMMARY_YAML)

    store = ClientTokenStore()
    store.register("client-a", token="tok-a")
    app = create_app(runtime=runtime, clients=store, pattern_catalog=pattern_catalog)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/submit",
            json={
                "client_id": "client-a",
                "agent": "distribute_summary",
                "payload": {"text": "The quick brown fox."},
            },
            headers=bearer("tok-a"),
        )
        assert resp.status_code == 200, resp.text
        task_id = resp.json()["task_id"]
        # The seed task deposits the root message on the node pump (§7.1).
        await runtime.manager.run()

        # The submit delivered to the composite's own class (dumb delivery point).
        # Run the composite fan-out, then the branch agents and the composite's
        # fan-in repeatedly, interleaving so the composite can collect returns.
        await comp.run()
        for _ in range(3):
            await summ.run()
            await cata.run()
            await comp.run()

        status_resp = await ac.get(f"/status/{task_id}", headers=bearer("tok-a"))
        for _ in range(10):
            await runtime.manager.run()
        status_resp = await ac.get(f"/status/{task_id}", headers=bearer("tok-a"))
        assert status_resp.status_code == 200, status_resp.text
        entry = status_resp.json()
        assert entry["state"] == "completed"
        assert entry["result_key"] == outbox_key(task_id)
        # the joined branches are gathered under the composite's output_as "result"
        assert entry["output"]["result"]["summ"]["summ"] == "a summary"
        assert entry["output"]["result"]["cata"]["cata"] == "a category"
