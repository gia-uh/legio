"""Contract tests for LEG-040 — CompositeAgent single branch (a sequence, R-4).

A composite with **one branch** is a sequence (AGENT_LIFECYCLE §4.10/§12.3):
steps run strictly in order and nothing returns to the composite itself. The
unified CompositeAgent fans the incoming request out to its single branch with
``level + 1`` and ``current_index = 0``, preserving ``task_id``,
``launcher_class`` and ``end_of_level_queue``, and re-keying the incoming payload
under the first step's ``input_as``; ``AgentBase`` then advances by position
through the branch's steps to the end of the level (Schema 2 §12.4). The same
code path handles the top-level sequence and the nested/branch sequence — there
is only one runner (LEG-044).

The contract asserts the two properties the LEG-040 acceptance requires:
ordering by the token (step 1's result is consumable in step 2) and payload
building across steps, plus the forwarding invariants for the composite. Failures
(an empty sequence) are never silent (AGENTS.md rule 9).
"""

from __future__ import annotations

import pytest
from beaver import AsyncBeaverDB

from legio.agents.base import AgentBase
from legio.agents.composite_agent import CompositeAgent
from legio.flow import ExecutionRequestMessage, ExecutionResultMessage, build_payload
from legio.naming import gathering_key, queue_key


class BuildStep(AgentBase):
    """One stage of a sequence: reads its input under ``input_as`` and constructs
    the new payload under ``output_as``, carrying the running ``value`` forward
    through the re-keying handoff to the next stage's ``input_as``."""

    def __init__(self, *, key: str, input_as: str, output_as: str, **kwargs: object) -> None:
        self._key = key
        self._input_as = input_as
        super().__init__(**kwargs, output_as=output_as)  # type: ignore[call-arg]

    async def _handle(self, request: ExecutionRequestMessage) -> dict:
        inp = dict(request.payload.get(self._input_as, {}))
        value = inp.get("value", 0)
        return build_payload({self._key: value, "value": value}, output_as=self._output_as)


async def pop_one(db: AsyncBeaverDB, agent_id: str) -> dict | None:
    try:
        item = await db.queue(queue_key(agent_id)).get(block=False)
    except IndexError:
        return None
    return item.data


class GatherComposite(CompositeAgent):
    """A concrete composite pattern: transcribes each branch's built payload
    (slot order) into its own ``output_as``.

    The engine's ``CompositeAgent`` has **no** generic default build — output
    construction is the pattern's model (it must satisfy the pattern's declared
    ``output_schema``). This fictitious-domain pattern picks the gather/
    transcription composition for its declared shape.
    """

    async def build_output_as(self, info):
        gathered: dict = {}
        for payload in info.values():
            gathered.update(payload)
        return build_payload(gathered, output_as=self._output_as)


def build_composite(
    *,
    db: AsyncBeaverDB,
    sequence_route: tuple[tuple[str, str], ...],
    agent_id: str = "seq",
    input_as: str = "seq",
    output_as: str = "seq",
) -> CompositeAgent:
    # A single-branch composite whose one branch is the sequence route.
    return GatherComposite(
        agent_id=agent_id,
        db=db,
        branches=[list(sequence_route)],
        input_as=input_as,
        output_as=output_as,
    )


def step_request(
    *,
    task_id: str,
    payload: dict,
    route: tuple[tuple[str, str], ...],
    current_index: int,
    end_of_level_queue: str,
    level: int = 1,
) -> ExecutionRequestMessage:
    return ExecutionRequestMessage(
        level_route=route,
        current_index=current_index,
        end_of_level_queue=end_of_level_queue,
        level=level,
        launcher_class=route[0][0] if route else "",
        task_id=task_id,
        payload=payload,
    )


@pytest.mark.asyncio
async def test_composite_forwards_to_first_stage_of_flattened_route(
    beaver_db: AsyncBeaverDB,
) -> None:
    """The composite re-seeds its first stage with current_index 0 and re-keys
    the incoming payload under the first stage's ``input_as``."""
    seq = build_composite(db=beaver_db, sequence_route=(("a", "a"), ("b", "b")), input_as="seq")
    request = step_request(
        task_id="T-seq",
        payload={"seq": {"seed": 7}},
        route=(("outer", "outer"), ("seq", "seq")),  # the sequence is step 1 of the outer route
        current_index=1,
        end_of_level_queue="result:T-seq",
    )
    await beaver_db.queue(queue_key("seq")).put(request.model_dump(mode="json"), priority=0.0)

    handled = await seq.process_next()
    assert handled is True

    first = await pop_one(beaver_db, "a")
    assert first is not None
    forwarded = ExecutionRequestMessage.model_validate(first)
    assert forwarded.level_route == (("a", "a"), ("b", "b"))
    assert forwarded.current_index == 0
    assert forwarded.task_id == "T-seq"
    # the branch's stages close to this composite's own gathering queue
    # (a second physical queue, gather:seq), so the composite can join them
    # before resuming its own level
    assert forwarded.end_of_level_queue == gathering_key("seq")
    assert forwarded.level == 2
    assert forwarded.payload == {"a": {"seed": 7}}


@pytest.mark.asyncio
async def test_composite_preserves_level_and_end_queue_for_branch(
    beaver_db: AsyncBeaverDB,
) -> None:
    """A single-branch composite inside a multi-branch composite's branch keeps
    its level and branch closer: the
    branch's stages return to the composite's own gathering queue, the composite
    joins them (single branch -> immediate) and resumes its own level carrying the
    outer-assigned closer (``gather:par``) and level."""
    seq = build_composite(
        db=beaver_db,
        sequence_route=(("p", "p"), ("q", "q")),
        input_as="seq",
        output_as="o",
    )
    step_p = BuildStep(agent_id="p", db=beaver_db, key="s", input_as="p", output_as="o")
    step_q = BuildStep(agent_id="q", db=beaver_db, key="t", input_as="o", output_as="o")
    request = step_request(
        task_id="T-branch",
        payload={"seq": {"v": 1}},
        route=(("par", "par"), ("seq", "seq")),
        current_index=1,
        end_of_level_queue="gather:par",  # the outer composite's gathering queue
        level=2,
    )
    await beaver_db.queue(queue_key("seq")).put(request.model_dump(mode="json"), priority=0.0)

    await seq.process_next()

    first = await pop_one(beaver_db, "p")
    assert first is not None
    forwarded = ExecutionRequestMessage.model_validate(first)
    assert forwarded.level == 3
    assert forwarded.end_of_level_queue == gathering_key("seq")
    assert forwarded.level_route == (("p", "p"), ("q", "q"))
    assert forwarded.payload == {"p": {"v": 1}}

    # run the branch to its close, then let the composite join and resume.
    await beaver_db.queue(queue_key("p")).put(forwarded.model_dump(mode="json"), priority=0.0)
    assert await step_p.process_next() is True
    second = await pop_one(beaver_db, "q")
    assert second is not None
    await beaver_db.queue(queue_key("q")).put(second, priority=0.0)
    assert await step_q.process_next() is True
    assert await seq.process_next() is True

    # the composite resumes its own level, preserving the outer branch closer.
    resumed = await pop_one(beaver_db, "gather:par")
    assert resumed is not None
    resumed_msg = ExecutionResultMessage.model_validate(resumed)
    assert resumed_msg.level == 2
    assert resumed_msg.end_of_level_queue == "gather:par"


@pytest.mark.asyncio
async def test_composite_runs_steps_in_order_and_builds_payload(
    beaver_db: AsyncBeaverDB,
) -> None:
    """A two-step sequence runs step 1 then step 2; step 2 consumes step 1's
    output via the re-keying handoff (ordering by the token) and the final
    result lands on the closer under the last stage's ``output_as``."""
    seq = build_composite(
        db=beaver_db,
        sequence_route=(("step1", "step1"), ("step2", "o1")),
        input_as="seq",
        output_as="o2",
    )
    step1 = BuildStep(agent_id="step1", db=beaver_db, key="s1", input_as="step1", output_as="o1")
    step2 = BuildStep(agent_id="step2", db=beaver_db, key="s2", input_as="o1", output_as="o2")
    request = step_request(
        task_id="T-2",
        payload={"seq": {"value": 3}},
        route=(("seq", "seq"),),
        current_index=0,
        end_of_level_queue="result:T-2",
    )
    await beaver_db.queue(queue_key("seq")).put(request.model_dump(mode="json"), priority=0.0)

    # Composite fans out to its single branch (first stage step1).
    assert await seq.process_next() is True

    first = await pop_one(beaver_db, "step1")
    assert first is not None
    first_request = ExecutionRequestMessage.model_validate(first)
    assert first_request.level_route == (("step1", "step1"), ("step2", "o1"))
    assert first_request.current_index == 0
    assert first_request.payload == {"step1": {"value": 3}}
    await beaver_db.queue(queue_key("step1")).put(
        first_request.model_dump(mode="json"), priority=0.0
    )
    assert await step1.process_next() is True

    second = await pop_one(beaver_db, "step2")
    assert second is not None
    second_request = ExecutionRequestMessage.model_validate(second)
    assert second_request.current_index == 1
    assert second_request.payload == {"o1": {"s1": 3, "value": 3}}
    await beaver_db.queue(queue_key("step2")).put(
        second_request.model_dump(mode="json"), priority=0.0
    )
    assert await step2.process_next() is True

    # The branch closes to the composite's gathering queue (its second physical
    # queue, separate from the class inbox); the composite joins (single branch
    # -> immediate) and resumes its own level — here the level's end, delivering
    # to the final-result queue.
    assert await seq.process_next() is True

    result_item = await pop_one(beaver_db, "result:T-2")
    assert result_item is not None
    result = ExecutionResultMessage.model_validate(result_item)
    assert result.task_id == "T-2"
    # the composite wraps the branch's produced output under its own output_as
    assert result.payload == {"o2": {"o2": {"s2": 3, "value": 3}}}


@pytest.mark.asyncio
async def test_composite_with_empty_route_fails_visibly(beaver_db: AsyncBeaverDB) -> None:
    """An empty sequence is a construction error, never silent."""
    seq = build_composite(db=beaver_db, sequence_route=())
    request = step_request(
        task_id="T-empty",
        payload={},
        route=(("seq", "seq"),),
        current_index=0,
        end_of_level_queue="result:T-empty",
    )
    await beaver_db.queue(queue_key("seq")).put(request.model_dump(mode="json"), priority=0.0)

    await seq.process_next()

    result_item = await pop_one(beaver_db, "result:T-empty")
    assert result_item is not None
    result = ExecutionResultMessage.model_validate(result_item)
    assert result.task_id == "T-empty"
    assert "error" in result.payload
