"""Contract tests for LEG-103 Slice 2 — pending-gather wakeup without a legio timer.

When a composite has a pending fan-out but both inlets are dry, the standing
tick must suspend on the class inbox with a bounded substrate wait (Option A):
inbox work/control wakes it immediately, then it re-polls gathering once. No
``asyncio.sleep`` may remain in the composite tick path — the wait is beaver's
own blocking ``get`` (the sanctioned consumption mechanism, not an engine
timer).
"""

from __future__ import annotations

import asyncio
import time

import pytest
from beaver import AsyncBeaverDB

from legio.agents.composite_agent import CompositeAgent
from legio.flow import ExecutionRequestMessage
from legio.naming import gathering_key, queue_key
from tests.test_leg041_multi_branch_composite import (
    GatherComposite,
    child_result,
    composite_request,
    pop_one,
)


def build_wakeup_composite(
    *, db: AsyncBeaverDB, gather_budget: float, branches=((("b1", "b1"),),),
    agent_id: str = "comp",
) -> CompositeAgent:
    routes: list[list[tuple[str, str]]] = [[step for step in branch] for branch in branches]
    return GatherComposite(
        agent_id=agent_id,
        db=db,
        branches=routes,
        input_as="main",
        output_as="comp",
        gather_budget=gather_budget,
    )


async def fan_out_one(db: AsyncBeaverDB, comp: CompositeAgent, task_id: str) -> None:
    request = composite_request(
        task_id=task_id,
        payload={"main": {"seed": 1}},
        route=(("main", "main"), ("comp", "comp"), ("after", "after")),
        current_index=1,
        end_of_level_queue="result:" + task_id,
    )
    await db.queue(queue_key("comp")).put(request.model_dump(mode="json"), priority=0.0)
    assert await comp.process_next() is True
    assert await comp._state.fetch(task_id) is not None


@pytest.mark.asyncio
async def test_pending_tick_waits_on_inbox_budget(beaver_db: AsyncBeaverDB) -> None:
    """Pending + dry inlets: the tick suspends on the inbox budget (~0.2s),
    it does not spin back in ~0.01s on a legio sleep."""
    comp = build_wakeup_composite(db=beaver_db, gather_budget=0.2)
    await fan_out_one(beaver_db, comp, "W-wait")

    start = time.monotonic()
    assert await comp._standing_tick() is True
    elapsed = time.monotonic() - start
    assert elapsed >= 0.15


@pytest.mark.asyncio
async def test_pending_tick_uses_no_legio_sleep(
    beaver_db: AsyncBeaverDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Record every asyncio.sleep during an idle pending tick: beaver's own
    0.1s substrate cadence may appear, but no sub-0.05s legio timer may."""
    calls: list[float] = []
    real_sleep = asyncio.sleep

    async def recording_sleep(delay: float, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        calls.append(delay)
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", recording_sleep)
    comp = build_wakeup_composite(db=beaver_db, gather_budget=0.25)
    await fan_out_one(beaver_db, comp, "W-nosleep")

    assert await comp._standing_tick() is True
    assert calls, "expected at least the substrate wait cadence"
    assert all(delay >= 0.05 for delay in calls), f"legio timer present: {calls}"


@pytest.mark.asyncio
async def test_pending_tick_collects_predeposited_gather_result(
    beaver_db: AsyncBeaverDB,
) -> None:
    """A gather result already waiting is still collected (fast path first),
    completing the single-branch join and resuming the level."""
    comp = build_wakeup_composite(db=beaver_db, gather_budget=0.2)
    await fan_out_one(beaver_db, comp, "W-join")
    record = await comp._state.fetch("W-join")
    assert record is not None
    branch_id = next(iter(record["slots"]))

    await beaver_db.queue(queue_key(gathering_key("comp"))).put(
        child_result(
            branch_id=branch_id,
            level_route=(("b1", "b1"),),
            task_id="W-join",
            payload={"b1": 7},
        ).model_dump(mode="json"),
        priority=0.0,
    )
    assert await comp._standing_tick() is True
    assert await comp._state.fetch("W-join") is None
    advanced = await pop_one(beaver_db, "after")
    assert advanced is not None
    assert ExecutionRequestMessage.model_validate(advanced).payload == {"after": {"b1": 7}}


@pytest.mark.asyncio
async def test_inbox_work_wakes_pending_tick_immediately(
    beaver_db: AsyncBeaverDB,
) -> None:
    """New inbox work while pending is fanned out at once — it never waits
    out the gather budget."""
    comp = build_wakeup_composite(db=beaver_db, gather_budget=0.5)
    await fan_out_one(beaver_db, comp, "W-first")
    second = composite_request(
        task_id="W-second",
        payload={"main": {"seed": 2}},
        route=(("main", "main"), ("comp", "comp"), ("after", "after")),
        current_index=1,
        end_of_level_queue="result:W-second",
    )
    await beaver_db.queue(queue_key("comp")).put(second.model_dump(mode="json"), priority=0.0)

    start = time.monotonic()
    assert await comp._standing_tick() is True
    elapsed = time.monotonic() - start
    assert elapsed < 0.4
    assert await comp._state.fetch("W-second") is not None


@pytest.mark.asyncio
async def test_nonpositive_budget_refused_loudly(beaver_db: AsyncBeaverDB) -> None:
    """A zero/negative budget would busy-spin: refuse it at construction."""
    with pytest.raises(ValueError, match="gather_budget"):
        build_wakeup_composite(db=beaver_db, gather_budget=0.0)
