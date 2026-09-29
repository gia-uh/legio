"""Contract tests for LEG-121 — a linguistic step is sent as `system` + `user`.

The role lives in the `system` turn (the pattern's `system_prompt`, else the
node config's general `system_prompt`, else empty); the task + its data live in
the `user` turn (the resolved `prompt`). The `prompt` is never duplicated into
the `system` turn. Spec: docs/CONTRACTS/LEG-121-system-and-user-turns.md.
"""

from __future__ import annotations

import pytest
from beaver import AsyncBeaverDB
from lingo.mock import MockLLM
from pydantic import BaseModel

from legio.agents.linguistic_agent import LinguisticAgent
from legio.flow import ExecutionRequestMessage
from legio.naming import queue_key


class SummarizeOutput(BaseModel):
    title: str
    summary: str


PROMPT = "Summarize {text} in {lang}."


def crafted_request(*, task_id: str, payload: dict) -> ExecutionRequestMessage:
    return ExecutionRequestMessage(
        level_route=(("main_a", "main_a"), ("summ", "summ")),
        current_index=1,
        end_of_level_queue="main_a",
        task_id=task_id,
        payload=payload,
    )


def build_agent(
    *,
    db: AsyncBeaverDB,
    lingo_client,
    prompt: str = PROMPT,
    system_prompt: str | None = None,
) -> LinguisticAgent:
    return LinguisticAgent(
        agent_id="summ",
        db=db,
        lingo_client=lingo_client,
        prompt_template=prompt,
        system_prompt_template=system_prompt,
        output_model=SummarizeOutput,
        input_as="summ",
        output_as="summ",
    )


async def _run(db: AsyncBeaverDB, agent: LinguisticAgent, task_id: str = "T") -> None:
    request = crafted_request(task_id=task_id, payload={"summ": {"text": "hello", "lang": "en"}})
    await db.queue(queue_key("summ")).put(request.model_dump(mode="json"), priority=0.0)
    await agent.process_next()


@pytest.mark.asyncio
async def test_two_turns_user_carries_prompt(beaver_db: AsyncBeaverDB) -> None:
    """system + user; the user turn carries the resolved prompt."""
    lingo_client = MockLLM(responses=[SummarizeOutput(title="t", summary="s")])
    agent = build_agent(db=beaver_db, lingo_client=lingo_client)
    await _run(beaver_db, agent)

    messages = lingo_client.history[-1]
    assert [m.role for m in messages] == ["system", "user"]
    assert messages[1].content == "Summarize hello in en."


@pytest.mark.asyncio
async def test_pattern_system_prompt_is_the_system_turn(beaver_db: AsyncBeaverDB) -> None:
    """A declared pattern `system_prompt` is the system turn (and can template)."""
    lingo_client = MockLLM(responses=[SummarizeOutput(title="t", summary="s")])
    agent = build_agent(
        db=beaver_db, lingo_client=lingo_client, system_prompt="You summarize in {lang}."
    )
    await _run(beaver_db, agent)

    messages = lingo_client.history[-1]
    assert messages[0].role == "system"
    assert messages[0].content == "You summarize in en."


@pytest.mark.asyncio
async def test_prompt_is_not_copied_into_system(beaver_db: AsyncBeaverDB) -> None:
    """Without a system_prompt, the system turn is empty — never the prompt."""
    lingo_client = MockLLM(responses=[SummarizeOutput(title="t", summary="s")])
    agent = build_agent(db=beaver_db, lingo_client=lingo_client)
    await _run(beaver_db, agent)

    messages = lingo_client.history[-1]
    assert messages[0].content == ""
    assert messages[0].content != messages[1].content


def _spec(**extra):
    base = {
        "name": "summ",
        "type": "atomic",
        "kind": "linguistic",
        "input": {
            "input_as": "summ",
            "input_type": "json",
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "lang": {"type": "string"}},
                "required": ["text", "lang"],
            },
        },
        "output": {
            "output_as": "summ",
            "output_type": "json",
            "output_schema": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "summary": {"type": "string"}},
                "required": ["title", "summary"],
            },
        },
        "prompt": "Summarize {text} in {lang}.",
    }
    base.update(extra)
    return base


def test_system_prompt_variables_are_checked() -> None:
    from legio.patterns import load_patterns

    with pytest.raises(Exception, match="undeclared variables"):
        load_patterns([_spec(system_prompt="You are missing {undefined_var}.")])


def test_system_prompt_with_declared_variables_loads() -> None:
    from legio.patterns import load_patterns

    catalog = load_patterns([_spec(system_prompt="You summarize in {lang}.")])
    assert "summ" in catalog
