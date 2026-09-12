"""Contract tests for LEG-023 — AgentBase.run() uniform loop (over native beaver).

The AgentBase is the generalized per-step runner every atomic agent and
composite implements (LEG-023). Its ``run()`` pops a work item from its native
beaver queue (destructively — no lease, no retry, no re-queue), dispatches it
to the step's job (a subclass ``_handle``), applies the ``monitor`` hook, and
routes the outcome by position (Schema 2): advance to the next class of the
level as an ``ExecutionRequestMessage`` or, at the end of the level, deposit an
``ExecutionResultMessage`` to the level's ``end_of_level_queue``. All substrate
is native beaver; queues are addressed by name on the shared db.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from beaver import AsyncBeaverDB

from legio.agents.base import AgentBase
from legio.flow import ExecutionRequestMessage, ExecutionResultMessage, build_payload
from legio.naming import queue_key


def make_request(
    *,
    task_id: str,
    current_index: int = 0,
    route: tuple[tuple[str, str], ...] = (("main", "main"),),
    end_of_level_queue: str = "client",
    level: int = 1,
    payload: dict | None = None,
) -> ExecutionRequestMessage:
    return ExecutionRequestMessage(
        level_route=route,
        current_index=current_index,
        end_of_level_queue=end_of_level_queue,
        level=level,
        task_id=task_id,
        payload={"main": {"v": 1}} if payload is None else payload,
    )


async def pop_one(db: AsyncBeaverDB, agent_id: str) -> dict | None:
    try:
        item = await db.queue(queue_key(agent_id)).get(block=False)
    except IndexError:
        return None
    return item.data


class FinalAgent(AgentBase):
    """Single/final step: reads its input under ``input_as`` and constructs its
    output (the builder's ``{output_as: output}``) allowing the base to finish."""

    def __init__(
        self,
        *,
        agent_id: str,
        db: Any,
        input_as: str | None = None,
        output_as: str | None = None,
        **kwargs: Any,
    ) -> None:
        self._input_as = input_as or agent_id
        super().__init__(agent_id=agent_id, db=db, output_as=output_as or agent_id, **kwargs)

    async def _handle(self, request: ExecutionRequestMessage) -> dict:
        inp = dict(request.payload.get(self._input_as, {}))
        return build_payload({"v": inp.get("v", 0)}, output_as=self._output_as)


class ChainAgent(AgentBase):
    """One link of a chain: read the value under ``input_as``, rebuild it
    incremented under ``output_as``; the base advances until level end."""

    def __init__(
        self,
        *,
        agent_id: str,
        db: Any,
        input_as: str | None = None,
        output_as: str | None = None,
        **kwargs: Any,
    ) -> None:
        self._input_as = input_as or agent_id
        super().__init__(agent_id=agent_id, db=db, output_as=output_as or agent_id, **kwargs)

    async def _handle(self, request: ExecutionRequestMessage) -> dict:
        inp = dict(request.payload.get(self._input_as, {}))
        return build_payload({"v": int(inp.get("v", 0)) + 1}, output_as=self._output_as)


class FailingAgent(AgentBase):
    """A broken step: raises in _handle, surfaced as a visible error result."""

    def __init__(self, **kwargs: object) -> None:
        self.failures: list[str] = []
        super().__init__(**kwargs)  # type: ignore[arg-type]

    async def _handle(self, request: ExecutionRequestMessage) -> dict:
        self.failures.append(request.task_id)
        raise ValueError("boom")


class BuildAgent(AgentBase):
    """A chain link: reads its input under ``input_as`` and constructs the new
    payload carrying a running ``s<stage>`` value plus ``value`` (the re-keying
    handoff carries it to the next step's ``input_as``)."""

    def __init__(
        self, *, agent_id: str, db: Any, input_as: str, output_as: str, **kwargs: Any
    ) -> None:
        self._input_as = input_as
        super().__init__(agent_id=agent_id, db=db, output_as=output_as, **kwargs)

    async def _handle(self, request: ExecutionRequestMessage) -> dict:
        inp = dict(request.payload.get(self._input_as, {}))
        stage = 1 + sum(1 for key in inp if key.startswith("s"))
        return build_payload({**inp, f"s{stage}": stage}, output_as=self._output_as)


@dataclass
class HookRecorder:
    events: list[tuple[str, str]] = field(default_factory=list)

    async def monitor(self, agent_id: str, task_id: str, event: str) -> None:
        self.events.append((event, task_id))


def build(agent_cls, *, agent_id: str, db: AsyncBeaverDB, **extra: object):
    return agent_cls(
        agent_id=agent_id,
        db=db,
        **extra,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_single_step_task_completes_with_result_in_place(beaver_db: AsyncBeaverDB) -> None:
    await beaver_db.queue(queue_key("main")).put(
        make_request(task_id="T-single").model_dump(mode="json"), priority=0.0
    )
    agent = build(FinalAgent, agent_id="main", db=beaver_db)

    steps = await agent.run()

    assert steps == 1
    result_item = await pop_one(beaver_db, "client")
    assert result_item is not None
    result = ExecutionResultMessage.model_validate(result_item)
    assert result.task_id == "T-single"
    assert result.payload["main"]["v"] == 1


@pytest.mark.asyncio
async def test_two_step_task_runs_both_steps_and_returns(beaver_db: AsyncBeaverDB) -> None:
    first = build(ChainAgent, agent_id="main", db=beaver_db)
    second = build(ChainAgent, agent_id="step", db=beaver_db)
    route = (("main", "main"), ("step", "step"))
    await beaver_db.queue(queue_key("main")).put(
        make_request(
            task_id="T-two", current_index=0, route=route, payload={"main": {"v": 1}}
        ).model_dump(mode="json"),
        priority=0.0,
    )

    assert await first.run() == 1
    step_item = await pop_one(beaver_db, "step")
    assert step_item is not None
    step_message = ExecutionRequestMessage.model_validate(step_item)
    assert step_message.task_id == "T-two"
    assert step_message.current_index == 1
    assert step_message.payload["step"]["v"] == 2
    await beaver_db.queue(queue_key("step")).put(step_message.model_dump(mode="json"), priority=0.0)

    assert await second.run() == 1
    ret_item = await pop_one(beaver_db, "client")
    assert ret_item is not None
    result = ExecutionResultMessage.model_validate(ret_item)
    assert result.task_id == "T-two"
    assert result.payload["step"]["v"] == 3


@pytest.mark.asyncio
async def test_broken_step_marks_task_failed_without_crash(beaver_db: AsyncBeaverDB) -> None:
    agent = build(FailingAgent, agent_id="main", db=beaver_db)
    await beaver_db.queue(queue_key("main")).put(
        make_request(task_id="T-bad").model_dump(mode="json"), priority=0.0
    )

    steps = await agent.run()

    assert steps == 1
    assert agent.failures == ["T-bad"]
    result_item = await pop_one(beaver_db, "client")
    assert result_item is not None
    result = ExecutionResultMessage.model_validate(result_item)
    assert result.task_id == "T-bad"
    assert "error" in result.payload





@pytest.mark.asyncio
async def test_monitor_hook_fires_on_step_events(beaver_db: AsyncBeaverDB) -> None:
    recorder = HookRecorder()
    agent = build(FinalAgent, agent_id="main", db=beaver_db)
    agent.set_hooks(monitor=recorder.monitor)
    await beaver_db.queue(queue_key("main")).put(
        make_request(task_id="T-mon").model_dump(mode="json"), priority=0.0
    )

    await agent.run()

    event_types = [event for event, _ in recorder.events]
    assert "start" in event_types
    assert "step_done" in event_types


@pytest.mark.asyncio
async def test_idle_queue_returns_zero_without_busy_loop(beaver_db: AsyncBeaverDB) -> None:
    agent = build(FinalAgent, agent_id="main", db=beaver_db)

    assert await agent.run() == 0


@pytest.mark.asyncio
async def test_run_drains_every_queued_item_no_arbitrary_cap(beaver_db: AsyncBeaverDB) -> None:
    """LEG-023: ``run()`` drains the queue to empty in one call.

    The only termination is the idle queue (LEG-070 cycles are rejected at
    load, routes are forward-only) — no hardcoded step cap may ever leave
    pending work behind silently (rule 9).
    """
    queue = beaver_db.queue(queue_key("main"))
    for suffix in ("T-a", "T-b", "T-c"):
        await queue.put(make_request(task_id=suffix).model_dump(mode="json"), priority=0.0)

    agent = build(FinalAgent, agent_id="main", db=beaver_db)

    assert await agent.run() == 3
    assert await queue.count() == 0


@pytest.mark.asyncio
async def test_root_task_deposits_result_to_end_of_level_queue(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Schema 2 (addendum AL): a root finish lands on ``end_of_level_queue``.

    The task's final-result queue (``result:...``) receives the result — never a
    ``client:`` queue (there is no ``client:`` family).
    """
    await beaver_db.queue(queue_key("main")).put(
        make_request(
            task_id="T-root", end_of_level_queue="result:T-root"
        ).model_dump(mode="json"),
        priority=0.0,
    )
    agent = build(FinalAgent, agent_id="main", db=beaver_db)

    assert await agent.run() == 1

    result_item = await pop_one(beaver_db, "result:T-root")
    assert result_item is not None
    result = ExecutionResultMessage.model_validate(result_item)
    assert result.payload["main"]["v"] == 1

    with pytest.raises(IndexError):
        await beaver_db.queue(queue_key("client:T-root")).get(block=False)


@pytest.mark.asyncio
async def test_three_stage_chain_builds_payload_across_steps(
    beaver_db: AsyncBeaverDB,
) -> None:
    """B1/C1: a chain of three builds the payload message-to-message.
    Each stage constructs its own payload under its ``output_as``; the re-keying
    handoff carries it under the next stage's ``input_as``, and the final step
    closes the level with its own ``output_as`` wrapper."""
    agents = [
        build(BuildAgent, agent_id=a, db=beaver_db, input_as=ia, output_as=oa)
        for a, ia, oa in (
            ("main", "main", "out1"),
            ("mid", "mid", "out2"),
            ("last", "last", "out3"),
        )
    ]
    route = (("main", "main"), ("mid", "mid"), ("last", "last"))
    await beaver_db.queue(queue_key("main")).put(
        make_request(
            task_id="T-chain",
            current_index=0,
            route=route,
            end_of_level_queue="result:T-chain",
            payload={"main": {}},
        ).model_dump(mode="json"),
        priority=0.0,
    )

    assert await agents[0].run() == 1
    mid_item = await pop_one(beaver_db, "mid")
    assert mid_item is not None
    mid_msg = ExecutionRequestMessage.model_validate(mid_item)
    assert mid_msg.payload == {"mid": {"s1": 1}}
    await beaver_db.queue(queue_key("mid")).put(mid_msg.model_dump(mode="json"), priority=0.0)

    assert await agents[1].run() == 1
    last_item = await pop_one(beaver_db, "last")
    assert last_item is not None
    last_msg = ExecutionRequestMessage.model_validate(last_item)
    assert last_msg.payload == {"last": {"s1": 1, "s2": 2}}
    await beaver_db.queue(queue_key("last")).put(last_msg.model_dump(mode="json"), priority=0.0)

    assert await agents[2].run() == 1
    row = await pop_one(beaver_db, "result:T-chain")
    assert row is not None
    result = ExecutionResultMessage.model_validate(row)
    assert result.payload == {"out3": {"s1": 1, "s2": 2, "s3": 3}}


@pytest.mark.asyncio
async def test_contract_check_accepts_superset_extra_keys(beaver_db: AsyncBeaverDB) -> None:
    """Superset semantics at the runner: declared data present, extras pass.

    The incoming payload carries a declared ``{"v": 1}`` plus an undeclared
    ``extra`` key; the built output matches the declared ``output_schema``.
    Both edges validate and the result is deposited normally.
    """
    await beaver_db.queue(queue_key("main")).put(
        make_request(
            task_id="T-superset", payload={"main": {"v": 1, "extra": "irrelevant"}}
        ).model_dump(mode="json"),
        priority=0.0,
    )
    agent = build(
        FinalAgent,
        agent_id="main",
        db=beaver_db,
        input_schema={"type": "object", "properties": {"v": {"type": "integer"}}},
        output_schema={"type": "object", "properties": {"v": {"type": "integer"}}},
    )

    assert await agent.run() == 1

    result_item = await pop_one(beaver_db, "client")
    assert result_item is not None
    result = ExecutionResultMessage.model_validate(result_item)
    assert result.payload["main"]["v"] == 1


@pytest.mark.asyncio
async def test_input_contract_rejection_routes_error(beaver_db: AsyncBeaverDB) -> None:
    """A strict input_schema violation is a visible error, never silent."""
    await beaver_db.queue(queue_key("main")).put(
        make_request(task_id="T-in-bad", payload={"main": {"v": "one"}}).model_dump(mode="json"),
        priority=0.0,
    )
    agent = build(
        FinalAgent,
        agent_id="main",
        db=beaver_db,
        input_schema={"type": "object", "properties": {"v": {"type": "integer"}}},
    )

    assert await agent.run() == 1

    result_item = await pop_one(beaver_db, "client")
    assert result_item is not None
    result = ExecutionResultMessage.model_validate(result_item)
    assert "error" in result.payload
    assert "input contract rejected" in result.payload["error"]


@pytest.mark.asyncio
async def test_output_contract_rejection_routes_error(beaver_db: AsyncBeaverDB) -> None:
    """A strict output_schema violation is a visible error, never silent."""
    await beaver_db.queue(queue_key("main")).put(
        make_request(task_id="T-out-bad").model_dump(mode="json"), priority=0.0
    )
    agent = build(
        FinalAgent,
        agent_id="main",
        db=beaver_db,
        # FinalAgent builds {"v": 1}; the declared contract says v is a string.
        output_schema={"type": "object", "properties": {"v": {"type": "string"}}},
    )

    assert await agent.run() == 1

    result_item = await pop_one(beaver_db, "client")
    assert result_item is not None
    result = ExecutionResultMessage.model_validate(result_item)
    assert "error" in result.payload
    assert "output contract rejected" in result.payload["error"]


# --- §12.5 deposit-time gate handshake (audit finding #1 fix) ----------------


@pytest.mark.asyncio
async def test_advance_into_a_disabled_class_is_a_visible_blocked_deposit(
    beaver_db: AsyncBeaverDB,
) -> None:
    """§12.5.1/§12.5.5: nothing enters a disabled class via the advancement rule.

    A non-error deposit that must advance into a closed gate is NOT delivered to
    that class; the step surfaces as a visible ``error`` result on its
    ``end_of_level_queue`` — never a silent drop (§12.5.5 "a blocked deposit is
    a visible failure").
    """
    await beaver_db.dict("gates").set("step", {"state": "disabled"})
    first = build(ChainAgent, agent_id="main", db=beaver_db)
    route = (("main", "main"), ("step", "step"))
    await beaver_db.queue(queue_key("main")).put(
        make_request(
            task_id="T-blocked",
            current_index=0,
            route=route,
            payload={"main": {"v": 1}},
        ).model_dump(mode="json"),
        priority=0.0,
    )

    assert await first.run() == 1

    # nothing entered the disabled class
    assert await pop_one(beaver_db, "step") is None
    # the blocked deposit surfaced on the level's end_of_level_queue, visibly
    result_item = await pop_one(beaver_db, "client")
    assert result_item is not None
    result = ExecutionResultMessage.model_validate(result_item)
    assert "error" in result.payload
    assert "disabled" in result.payload["error"]


@pytest.mark.asyncio
async def test_error_result_deposit_is_exempt_from_a_closed_gate(
    beaver_db: AsyncBeaverDB,
) -> None:
    """§12.5.5 exemption: an error-result deposit always proceeds past a gate.

    A failing step's error payload must reach its return path even through a
    closed class — the advance is NOT blocked, the error drains (policy A).
    """
    await beaver_db.dict("gates").set("step", {"state": "disabled"})
    agent = build(FailingAgent, agent_id="main", db=beaver_db)
    route = (("main", "main"), ("step", "step"))
    await beaver_db.queue(queue_key("main")).put(
        make_request(
            task_id="T-exempt",
            current_index=0,
            route=route,
            payload={"main": {"v": 1}},
        ).model_dump(mode="json"),
        priority=0.0,
    )

    assert await agent.run() == 1

    # the error advanced into the closed class (exempt) — never stalled
    step_item = await pop_one(beaver_db, "step")
    assert step_item is not None
    step_msg = ExecutionRequestMessage.model_validate(step_item)
    assert "error" in step_msg.payload
