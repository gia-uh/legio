"""Contract tests for LEG-103 Slice 3 — fan-in without shared-mutable records.

Each worker writes only its own slot key; the join closes exactly once via one
atomic continuation-delete (first deleter wins, the loser stands down). No
locks, no TTL, no timers.
"""

from __future__ import annotations

import asyncio

import pytest
from beaver import AsyncBeaverDB

from legio.flow import ExecutionRequestMessage
from legio.naming import gathering_key, queue_key
from tests.test_leg041_multi_branch_composite import (
    GatherComposite,
    child_result,
    composite_request,
    pop_one,
)


def build_join_composite(*, db: AsyncBeaverDB) -> GatherComposite:
    return GatherComposite(
        agent_id="comp",
        db=db,
        branches=[[("b1", "b1")], [("b2", "b2")]],
        input_as="main",
        output_as="comp",
    )


async def fan_out_task(db: AsyncBeaverDB, comp: GatherComposite, task_id: str) -> None:
    request = composite_request(
        task_id=task_id,
        payload={"main": {"seed": 1}},
        route=(("main", "main"), ("comp", "comp"), ("after", "after")),
        current_index=1,
        end_of_level_queue="result:" + task_id,
    )
    await db.queue(queue_key("comp")).put(request.model_dump(mode="json"), priority=0.0)
    assert await comp.process_next() is True


async def pop_branch_id(db: AsyncBeaverDB, branch_class: str) -> str:
    item = await pop_one(db, branch_class)
    assert item is not None
    return ExecutionRequestMessage.model_validate(item).branch_id


async def deposit_result(
    db: AsyncBeaverDB, *, branch_id: str, route: tuple, task_id: str, payload: dict
) -> dict:
    message = child_result(
        branch_id=branch_id, level_route=route, task_id=task_id, payload=payload
    ).model_dump(mode="json")
    await db.queue(queue_key(gathering_key("comp"))).put(message, priority=0.0)
    return message


@pytest.mark.asyncio
async def test_join_layout_is_per_slot_keys(beaver_db: AsyncBeaverDB) -> None:
    """After fan-out the continuation carries the ordered expected slot list
    (no embedded shared-mutable slots), and each slot lives under its own key
    with its fan-out index and an empty result."""
    comp = build_join_composite(db=beaver_db)
    await fan_out_task(beaver_db, comp, "T-lay")

    record = await comp._state.fetch("T-lay")
    assert record is not None
    assert "slots" not in record
    assert len(record["expected"]) == 2

    for index, branch_class in enumerate(("b1", "b2")):
        branch_id = await pop_branch_id(beaver_db, branch_class)
        assert branch_id in record["expected"]
        slot = await comp._slots.fetch(f"T-lay:{branch_id}")
        assert slot == {"index": index, "result": None}


@pytest.mark.asyncio
async def test_concurrent_branch_returns_join_exactly_once(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Two branch results processed concurrently join exactly once: one
    advance, no stranded record, no leftover slots. The shared-mutable layout
    loses a slot here (last-writer-wins) and strands the join."""
    comp = build_join_composite(db=beaver_db)
    await fan_out_task(beaver_db, comp, "T-c")
    first_id = await pop_branch_id(beaver_db, "b1")
    second_id = await pop_branch_id(beaver_db, "b2")

    first = await deposit_result(
        beaver_db,
        branch_id=first_id,
        route=(("b1", "b1"),),
        task_id="T-c",
        payload={"b1": 1},
    )
    second = await deposit_result(
        beaver_db,
        branch_id=second_id,
        route=(("b2", "b2"),),
        task_id="T-c",
        payload={"b2": 2},
    )
    await asyncio.gather(
        comp._process_join_item(dict(first)),
        comp._process_join_item(dict(second)),
    )

    advanced = await pop_one(beaver_db, "after")
    assert advanced is not None
    assert ExecutionRequestMessage.model_validate(advanced).payload == {"after": {"b1": 1, "b2": 2}}
    assert await pop_one(beaver_db, "after") is None
    assert await comp._state.fetch("T-c") is None
    assert await comp._slots.fetch(f"T-c:{first_id}") is None
    assert await comp._slots.fetch(f"T-c:{second_id}") is None


@pytest.mark.asyncio
async def test_unknown_branch_still_loud(beaver_db: AsyncBeaverDB) -> None:
    """A result naming a branch the fan-out never created stays a visible
    error, never a silent slot."""
    comp = build_join_composite(db=beaver_db)
    await fan_out_task(beaver_db, comp, "T-unknown")
    message = await deposit_result(
        beaver_db,
        branch_id="nope",
        route=(("b1", "b1"),),
        task_id="T-unknown",
        payload={"b1": 1},
    )
    with pytest.raises(ValueError, match="unknown branch"):
        await comp._process_join_item(dict(message))


@pytest.mark.asyncio
async def test_result_without_fanout_still_loud(beaver_db: AsyncBeaverDB) -> None:
    """A gather result with no pending fan-out stays a visible anomaly."""
    comp = build_join_composite(db=beaver_db)
    message = await deposit_result(
        beaver_db,
        branch_id="ghost",
        route=(("b1", "b1"),),
        task_id="T-ghost",
        payload={"b1": 1},
    )
    with pytest.raises(ValueError, match="no fan-out"):
        await comp._process_join_item(dict(message))
