"""Contract tests for LEG-095 Phase 2 — per-agent result queue + outbox intake.

The final-result queue is per served root agent (``result:<agent>``, shared by
every task starting there), not per task. A Runtime intake (``RESULT_DRAIN``)
consumes ``ExecutionResultMessage``s into per-task ``outbox`` records;
``status`` / ``read_outbox`` / ``ack_outbox`` read records, never the physical
queue (LEG-095 Phase 2 contract).
"""

from __future__ import annotations

import logging

import pytest
from beaver import AsyncBeaverDB

from legio.flow import ExecutionResultMessage, FlowToken
from legio.naming import outbox_key, queue_key, result_queue_key
from legio.patterns import load_patterns
from legio.runtime import RESULT_DRAIN_TASK, Runtime, TaskState

AGENT = "cutter"


def _agent_yaml(name: str) -> str:
    return f"""\
name: {name}
type: atomic
kind: tool
input:
  input_as: {name}
  input_type: json
  input_schema:
    type: object
    properties:
      raw: {{type: string}}
output:
  output_as: {name}
  output_type: json
  output_schema:
    type: object
    properties:
      raw: {{type: string}}
tool: {name}
parameters:
  raw: "{{{name}.raw}}"
"""


def _agent_spec(name: str):
    return load_patterns(_agent_yaml(name)).specs[name]


def _flow_result(
    task_id: str, agent: str = AGENT, *, payload: dict | None = None
) -> ExecutionResultMessage:
    """A flow-end result as the agent's ``_deposit_result`` writes it."""
    return ExecutionResultMessage(
        end_of_level_queue=result_queue_key(agent),
        level=1,
        launcher_class=agent,
        task_id=task_id,
        branch_id="branch-for-test",
        payload=payload or {agent: {"raw": "cut"}},
    )


async def _pump(runtime: Runtime, rounds: int = 10) -> int:
    """Dispatch pending Manager tasks until idle (the node's executor)."""
    dispatched = 0
    for _ in range(rounds):
        step = await runtime.manager.run()
        if step == 0:
            break
        dispatched += step
    return dispatched


async def _seed_token(runtime: Runtime, task_id: str) -> FlowToken:
    record = await runtime.manager.status(task_id)
    assert record is not None
    return FlowToken.model_validate(record.kwargs["token"])


# --------------------------------------------------------------------------
# § naming: one queue per root agent, one record pointer per task
# --------------------------------------------------------------------------


def test_result_queue_key_is_per_agent() -> None:
    assert result_queue_key("cutter") == "result:cutter"
    assert result_queue_key("cutter") != result_queue_key("carve")


def test_outbox_key_addresses_the_task_record() -> None:
    tid = "author@test:11111111-1111-1111-1111-111111111111"
    assert outbox_key(tid) == f"outbox:{tid}"


# --------------------------------------------------------------------------
# § submit seeds the per-agent queue; tasks share it
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_seeds_per_agent_result_queue(
    beaver_db: AsyncBeaverDB, runtime: Runtime
) -> None:
    first = await runtime.submit("client-a", ((AGENT, AGENT),), {"raw": "one"})
    second = await runtime.submit("client-a", ((AGENT, AGENT),), {"raw": "two"})
    assert (await _seed_token(runtime, first)).end_of_level_queue == result_queue_key(AGENT)
    assert (await _seed_token(runtime, second)).end_of_level_queue == result_queue_key(AGENT)
    assert (await _seed_token(runtime, first)).end_of_level_queue == (
        await _seed_token(runtime, second)
    ).end_of_level_queue


# --------------------------------------------------------------------------
# § drain collects into the record; status completes via the record
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flow_end_result_collected_into_outbox_record(
    beaver_db: AsyncBeaverDB, runtime: Runtime
) -> None:
    task_id = await runtime.submit("client-a", ((AGENT, AGENT),), {"raw": "one"})
    await _pump(runtime)  # seed deposits the root request; submit-kick drain no-ops
    await beaver_db.queue(queue_key(result_queue_key(AGENT))).put(
        _flow_result(task_id).model_dump(mode="json"), priority=0.0
    )

    pending = await runtime.status(task_id, "client-a")  # kicks the drain, record absent
    assert pending.state is not TaskState.COMPLETED

    await _pump(runtime)  # the kicked drain records the result
    entry = await runtime.status(task_id, "client-a")
    assert entry.state is TaskState.COMPLETED
    assert entry.output == {AGENT: {"raw": "cut"}}
    assert entry.result_key == outbox_key(task_id)


@pytest.mark.asyncio
async def test_two_tasks_on_one_agent_have_isolated_records(
    beaver_db: AsyncBeaverDB, runtime: Runtime
) -> None:
    first = await runtime.submit("client-a", ((AGENT, AGENT),), {"raw": "one"})
    second = await runtime.submit("client-a", ((AGENT, AGENT),), {"raw": "two"})
    await _pump(runtime)
    await beaver_db.queue(queue_key(result_queue_key(AGENT))).put(
        _flow_result(first, payload={AGENT: {"raw": "first"}}).model_dump(mode="json"),
        priority=0.0,
    )
    await beaver_db.queue(queue_key(result_queue_key(AGENT))).put(
        _flow_result(second, payload={AGENT: {"raw": "second"}}).model_dump(mode="json"),
        priority=0.0,
    )
    await runtime.status(first, "client-a")  # kick on miss
    await runtime.status(second, "client-a")  # kick on miss
    await _pump(runtime, rounds=20)

    first_entry = await runtime.status(first, "client-a")
    second_entry = await runtime.status(second, "client-a")
    assert first_entry.output == {AGENT: {"raw": "first"}}
    assert second_entry.output == {AGENT: {"raw": "second"}}
    assert first_entry.result_key == outbox_key(first)
    assert second_entry.result_key == outbox_key(second)


@pytest.mark.asyncio
async def test_drain_rekicks_while_items_remain(beaver_db: AsyncBeaverDB, runtime: Runtime) -> None:
    task_id = await runtime.submit("client-a", ((AGENT, AGENT),), {"raw": "one"})
    await _pump(runtime)
    queue = beaver_db.queue(queue_key(result_queue_key(AGENT)))
    await queue.put(
        _flow_result(task_id, payload={AGENT: {"raw": "a"}}).model_dump(mode="json"),
        priority=0.0,
    )
    await queue.put(
        _flow_result(task_id, payload={AGENT: {"raw": "b"}}).model_dump(mode="json"),
        priority=0.0,
    )
    await runtime.status(task_id, "client-a")  # exactly one kick on miss
    await _pump(runtime, rounds=20)
    assert await queue.count() == 0  # both items consumed across re-kicks
    entry = await runtime.status(task_id, "client-a")
    assert entry.state is TaskState.COMPLETED


# --------------------------------------------------------------------------
# § invalid items are consumed with a visible WARNING, never applied
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_item_on_result_queue_consumed_with_warning(
    beaver_db: AsyncBeaverDB, runtime: Runtime, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    task_id = await runtime.submit("client-a", ((AGENT, AGENT),), {"raw": "one"})
    queue = beaver_db.queue(queue_key(result_queue_key(AGENT)))
    await queue.put({"not": "a result message"}, priority=0.0)
    await _pump(runtime)  # the submit-kick drain meets the garbage first
    assert await queue.count() == 0
    assert "result_drain invalid" in caplog.text
    assert await beaver_db.dict("outbox").fetch(task_id) is None
    entry = await runtime.status(task_id, "client-a")
    assert entry.state is not TaskState.COMPLETED


# --------------------------------------------------------------------------
# § ack consumes the record; ack-on-empty is a no-op; ack never kicks
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ack_consumes_record_and_reread_is_empty(
    beaver_db: AsyncBeaverDB, runtime: Runtime
) -> None:
    task_id = await runtime.submit("client-a", ((AGENT, AGENT),), {"raw": "one"})
    await _pump(runtime)
    await beaver_db.queue(queue_key(result_queue_key(AGENT))).put(
        _flow_result(task_id).model_dump(mode="json"), priority=0.0
    )
    await runtime.status(task_id, "client-a")
    await _pump(runtime)
    assert await runtime.read_outbox(task_id) == {AGENT: {"raw": "cut"}}

    assert await runtime.ack_outbox(task_id) is True
    assert await runtime.read_outbox(task_id) is None
    assert await runtime.ack_outbox(task_id) is False


@pytest.mark.asyncio
async def test_ack_without_poll_returns_false_without_losing_result(
    beaver_db: AsyncBeaverDB, runtime: Runtime
) -> None:
    task_id = await runtime.submit("client-a", ((AGENT, AGENT),), {"raw": "one"})
    await _pump(runtime)
    await beaver_db.queue(queue_key(result_queue_key(AGENT))).put(
        _flow_result(task_id).model_dump(mode="json"), priority=0.0
    )
    # No poll happened, so nothing was collected: ack is a no-op, not a loss.
    assert await runtime.ack_outbox(task_id) is False
    await runtime.status(task_id, "client-a")  # the poll kicks the drain
    await _pump(runtime)
    entry = await runtime.status(task_id, "client-a")
    assert entry.state is TaskState.COMPLETED


@pytest.mark.asyncio
async def test_read_outbox_unknown_task_returns_none(
    beaver_db: AsyncBeaverDB, runtime: Runtime
) -> None:
    assert await runtime.read_outbox("ghost@test:00000000-0000-0000-0000-000000000000") is None


# --------------------------------------------------------------------------
# § destroy_class clears the agent's result queue
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_destroy_class_clears_agent_result_queue(
    beaver_db: AsyncBeaverDB, runtime: Runtime
) -> None:
    await runtime.create_class(_agent_spec("doomed"), pool=0)
    queue = beaver_db.queue(queue_key(result_queue_key("doomed")))
    await queue.put({"stale": "result"}, priority=0.0)
    await runtime.destroy_class("doomed", mode="now")
    assert await queue.count() == 0


# --------------------------------------------------------------------------
# § footprint pin (rule 13): the Phase-2 share is exactly the outbox dict +
#   the agent's class/result queues
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase2_footprint_is_outbox_plus_agent_queues(
    beaver_db: AsyncBeaverDB, runtime: Runtime
) -> None:
    """Rule 13: a submit → flow-end → collect cycle touches exactly the
    Manager's `tasks`, the agent's class + shared result queues (flow medium),
    and the Runtime-owned `outbox` dict — no other layer's scope, no per-task
    queue. Fully-drained queues leave no residue (beaver lists only queues
    that still hold items)."""
    task_id = await runtime.submit("client-a", ((AGENT, AGENT),), {"raw": "one"})
    await _pump(runtime)
    await beaver_db.queue(queue_key(result_queue_key(AGENT))).put(
        _flow_result(task_id).model_dump(mode="json"), priority=0.0
    )

    async def _scopes() -> tuple[set[str], set[str]]:
        cursor = await beaver_db.connection.execute(
            "SELECT DISTINCT dict_name FROM __beaver_dicts__"
        )
        dicts = {row[0] for row in await cursor.fetchall()}
        cursor = await beaver_db.connection.execute(
            "SELECT DISTINCT queue_name FROM __beaver_priority_queues__"
        )
        return dicts, {row[0] for row in await cursor.fetchall()}

    _, queues_before = await _scopes()
    assert queue_key(result_queue_key(AGENT)) in queues_before

    await runtime.status(task_id, "client-a")
    await _pump(runtime)

    dicts, queues = await _scopes()
    assert dicts == {"tasks", "outbox"}
    assert queue_key(result_queue_key(AGENT)) not in queues  # drained, no residue
    assert queue_key(AGENT) in queues  # the root request still awaits its agent


# --------------------------------------------------------------------------
# § the drain is a registered Manager fact (pinning the intake seam)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_drain_fact_is_registered(beaver_db: AsyncBeaverDB, runtime: Runtime) -> None:
    assert RESULT_DRAIN_TASK == "result_drain"
    task_id = await runtime.manager.submit_task(RESULT_DRAIN_TASK, AGENT)
    assert await runtime.manager.run() == 1
    record = await runtime.manager.status(task_id)
    assert record is not None
    assert record.status.value == "success"
