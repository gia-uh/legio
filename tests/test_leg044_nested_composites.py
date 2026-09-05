"""Contract tests for LEG-044 — nested composites (R-4, domain-free).

Fase 3 step 8 / LEG-044 Accept: "nested composite branches (multi-step,
**composite-in-branch**) run correctly", plus LEG-051 (the nested composite
returns to the *exact parent* that deposited it).

`deep_pipeline` is a composite root with two branches:
- branch 0 = ``[extract_and_summarize]`` — a **composite as a step** (reuse by
  reference, recursion): the inner composite fans out its own multi-step branch
  ``extract → assess`` at a deeper level and gathers it back.
- branch 1 = ``[cata]`` — an atomic, joined alongside.

The assertions prove the recursion mechanics: the inner composite fans out at
level 3, its leaf closes back into it, it joins and returns only to its exact
parent ``deep_pipeline`` (branch close ``level=2 to=deep_pipeline``), and the
root finishes and delivers to the final-result queue.
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
from legio.naming import result_queue_key
from legio.patterns import load_patterns, resolve_composite_branches
from legio.security import ClientTokenStore
from legio.tools import AvailableToolsRegistry

NESTED_YAML = """
---
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
---
name: extract_and_summarize
type: composite
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
name: deep_pipeline
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
  output_as: final
  output_type: json
  output_schema:
    type: object
    properties:
      result: {type: object}
      cata: {type: object}
branches:
  - - extract_and_summarize
  - - cata
"""


class ExtractOutput(BaseModel):
    title: str
    summary: str
    word_count: int = 0


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


def build_nested_agents(
    db: AsyncBeaverDB,
) -> tuple[CompositeAgent, CompositeAgent, LinguisticAgent, ToolAgent, LinguisticAgent]:
    """Boot the outer composite, the inner composite and the standing atomics."""
    registry = AvailableToolsRegistry()
    registry.declare(
        "assess",
        implementation="tests.test_leg044_nested_composites.fake_assess",
        policy={"timeout": 30, "retries": 0},
    )

    catalog = load_patterns(NESTED_YAML)
    extract_spec = catalog.specs["extract"]
    assess_spec = catalog.specs["assess"]
    cata_spec = catalog.specs["cata"]
    inner_spec = catalog.specs["extract_and_summarize"]
    outer_spec = catalog.specs["deep_pipeline"]
    extract = LinguisticAgent(
        agent_id="extract",
        db=db,
        lingo_client=MockLLM(
            responses=[ExtractOutput(title="Foxes", summary="A note about foxes.", word_count=4)]
        ),
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

    inner = GatherComposite(
        agent_id="extract_and_summarize",
        db=db,
        branches=resolve_composite_branches(inner_spec, catalog),
        input_as=inner_spec.input.input_as,
        output_as=inner_spec.output.output_as,
        input_schema=inner_spec.input.input_schema,
        output_schema=inner_spec.output.output_schema,
    )
    outer = GatherComposite(
        agent_id="deep_pipeline",
        db=db,
        branches=resolve_composite_branches(outer_spec, catalog),
        input_as=outer_spec.input.input_as,
        output_as=outer_spec.output.output_as,
        input_schema=outer_spec.input.input_schema,
        output_schema=outer_spec.output.output_schema,
    )
    return outer, inner, extract, assess, cata


def test_nested_branch_resolution() -> None:
    """A composite step stays a position in the branch route (bare name)."""
    catalog = load_patterns(NESTED_YAML)
    outer = catalog.specs["deep_pipeline"]
    branches = resolve_composite_branches(outer, catalog)
    assert branches == [
        (("extract_and_summarize", "payload"),),
        (("cata", "text"),),
    ]


@pytest.mark.asyncio
async def test_nested_composite_in_branch_over_rest(
    caplog: pytest.LogCaptureFixture, beaver_db: AsyncBeaverDB
) -> None:
    caplog.set_level(logging.INFO)
    outer, inner, extract, assess, cata = build_nested_agents(beaver_db)
    pattern_catalog = load_patterns(NESTED_YAML)

    store = ClientTokenStore()
    store.register("client-a", token="tok-a")
    app = create_app(clients=store, pattern_catalog=pattern_catalog)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/submit",
            json={
                "client_id": "client-a",
                "agent": "deep_pipeline",
                "payload": {"text": "The quick brown fox.", "lang": "en"},
            },
            headers=bearer("tok-a"),
        )
        assert resp.status_code == 200, resp.text
        task_id = resp.json()["task_id"]

        # Outer fan-out, then drain: inner composite (recursion), leaves, joins,
        # and the nested composite returning to its exact parent. Idle runs are
        # harmless (agents no-op on an empty queue).
        await outer.run()
        for _ in range(6):
            await inner.run()
            await extract.run()
            await assess.run()
            await cata.run()
            await outer.run()

        status_resp = await ac.get(f"/status/{task_id}", headers=bearer("tok-a"))
        assert status_resp.status_code == 200, status_resp.text
        entry = status_resp.json()
        assert entry["state"] == "completed"
        assert entry["result_key"] == result_queue_key(task_id)
        # The outer composite gathered its two branches under its output_as:
        # slot 1 is the nested composite's own built payload {result: {result: ...}}.
        assert (
            entry["output"]["final"]["result"]["result"]["result"] == "[Foxes] A note about foxes."
        )
        assert entry["output"]["final"]["cata"]["cata"] == "a category"

    # Recursion mechanics are observable in the log stream (rule 12 audit trail):
    # the inner composite fanned out its own multi-step branch at a deeper level.
    assert "composite fan-out agent=extract_and_summarize task=" in caplog.text
    assert "agent run agent=extract task=" in caplog.text and "level=3 index=0" in caplog.text
    # LEG-051: the nested composite returns only to its exact parent and closes
    # at level 2 (its own level), not at the root.
    assert "agent branch close agent=extract_and_summarize task=" in caplog.text
    assert "level=2 to=deep_pipeline" in caplog.text
    # The root composite finishes and delivers to the final-result queue.
    assert "agent finish agent=deep_pipeline task=" in caplog.text
