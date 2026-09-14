"""Tests for LEG-014 — Mini-manager (over native beaver).

These tests pin the public contract of the business submit/status surfaced by
the ``Runtime`` (LEG-085, §7.1/§7.7): async ``submit`` / ``status`` over the
native beaver ``tasks`` dictionary (the TaskRegistry) and the per-agent queues
(no invented substrate), the root result landing on the starting agent's
shared final-result queue (Schema 2 ``end_of_level_queue``, collected into the
per-task outbox record and read via ``status``), task ownership tagging (per
LEG-017) and root-token semantics (per LEG-011). There is no ``results`` store
and no per-task ``client:`` queue (addendum AL).
"""

from __future__ import annotations

import pytest
from beaver import AsyncBeaverDB

from legio.flow import ExecutionRequestMessage, ExecutionResultMessage, FlowToken
from legio.naming import outbox_key, queue_key, result_queue_key
from legio.runtime import Runtime, TaskState


@pytest.mark.asyncio
async def test_submit_returns_task_id_and_tags_owner(
    beaver_db: AsyncBeaverDB, runtime: Runtime
) -> None:
    task_id = await runtime.submit(
        "client-a", (("flow_alpha", "flow_alpha"),), {"raw": 1}
    )
    assert isinstance(task_id, str)
    assert task_id

    entry = await runtime.status(task_id, "client-a")
    assert entry.task_id == task_id
    assert entry.owner == "client-a"
    assert entry.output is None


@pytest.mark.asyncio
async def test_root_result_lands_in_final_result_queue_readable_via_status(
    beaver_db: AsyncBeaverDB,
    runtime: Runtime,
) -> None:
    task_id = await runtime.submit(
        "client-a", (("flow_alpha", "flow_alpha"),), {"raw": 1}
    )

    result = ExecutionResultMessage(
        task_id=task_id,
        level_route=(("flow_alpha", "flow_alpha"),),
        current_index=0,
        end_of_level_queue=result_queue_key("flow_alpha"),
        payload={"raw": 1, "kind": "ok"},
    )
    await beaver_db.queue(queue_key(result_queue_key("flow_alpha"))).put(
        result.model_dump(mode="json"), priority=0.0
    )

    # Status schedules collection on miss; the node pump dispatches the drain.
    await runtime.status(task_id, "client-a")
    for _ in range(10):
        await runtime.manager.run()

    entry = await runtime.status(task_id, "client-a")
    assert entry.output == {"raw": 1, "kind": "ok"}
    assert entry.result_key == outbox_key(task_id)
    assert entry.state is TaskState.COMPLETED


@pytest.mark.asyncio
async def test_status_is_scoped_to_the_owning_client(
    beaver_db: AsyncBeaverDB,
    runtime: Runtime,
) -> None:
    task_id = await runtime.submit(
        "client-a", (("flow_alpha", "flow_alpha"),), {"raw": 1}
    )

    with pytest.raises(PermissionError):
        await runtime.status(task_id, "client-b")

    with pytest.raises(PermissionError):
        await runtime.status(task_id, None)


@pytest.mark.asyncio
async def test_submitted_task_holds_root_token_with_result_queue_target(
    beaver_db: AsyncBeaverDB,
    runtime: Runtime,
) -> None:
    task_id = await runtime.submit(
        "client-a", (("flow_alpha", "flow_alpha"),), {"raw": 1}
    )

    entry = await runtime.status(task_id, "client-a")
    token = entry.token
    assert isinstance(token, FlowToken)
    assert token.root is True
    assert token.task_id == task_id
    assert token.level == 1
    assert token.end_of_level_queue == result_queue_key("flow_alpha")
    # ARCHITECTURE §3: the submit assigns the root branch_id — every message of
    # the flow carries a non-empty branch_id, never empty.
    assert token.branch_id
    # The root message is deposited by the seed task the node pump runs
    # (ARCHITECTURE §7.1): one ``manager.run()`` dispatch lands it in the
    # starting agent's queue, carrying the same root id.
    await runtime.manager.run()
    item = await beaver_db.queue(queue_key("flow_alpha")).get(block=False)
    root_request = ExecutionRequestMessage.model_validate(item.data)
    assert root_request.branch_id == token.branch_id


@pytest.mark.asyncio
async def test_submitted_task_stays_pending_until_root_result(
    beaver_db: AsyncBeaverDB,
    runtime: Runtime,
) -> None:
    task_id = await runtime.submit(
        "client-a", (("flow_alpha", "flow_alpha"),), {"raw": 1}
    )

    entry = await runtime.status(task_id, "client-a")
    assert entry.state is TaskState.PENDING
    assert entry.result_key is None