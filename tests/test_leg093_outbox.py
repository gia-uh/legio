"""Contract tests for LEG-093 — outbox polling by the author (write-before-ack, idempotency).

Result readback for remote work: the author polls the acceptor's outbox for the
result of a deposited work item; ack consumes (read-after-ack = empty). The
deposit is idempotent: a duplicate work item with the same id is never executed
twice.

In the modern triangle the acceptor's outbox is the per-task **record** in the
``outbox`` dict: the agent flow writes the ``ExecutionResultMessage`` onto the
agent's shared result queue (``result:<agent>``, LEG-095 Phase 2) at the end of
level 1, *during* execution, and the ``RESULT_DRAIN`` intake collects it into
the record — write-before-ack stays structural (the write precedes any ack),
and the two endpoints ``GET /outbox/{task_id}`` (non-blocking poll) and
``DELETE /outbox/{task_id}`` (destructive ack) are a thin read/consume shell
over the record (LEG-093 contract).
"""

from __future__ import annotations

import logging

import httpx
import pytest
from beaver import AsyncBeaverDB

from legio.flow import ExecutionResultMessage
from legio.naming import queue_key, result_queue_key
from legio.patterns import Catalog, load_patterns
from legio.runtime import Runtime

logger = logging.getLogger("legio.tests.leg093")

NODE_ID = "recipient@test"
AUTHOR_NODE = "author@test"

TOOL_YAML = """\
name: cutter
type: atomic
kind: tool
input:
  input_as: payload
  input_type: json
  input_schema:
    type: object
    properties:
      raw: {type: string}
output:
  output_as: result
  output_type: json
  output_schema:
    type: object
    properties:
      result: {type: string}
tool: cutter
parameters:
  raw: "{payload.raw}"
"""


def capacity_catalog() -> Catalog:
    return load_patterns(TOOL_YAML)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def task_id(suffix: str = "11111111-1111-1111-1111-111111111111") -> str:
    return f"{AUTHOR_NODE}:{suffix}"


def build_app(
    beaver_db: AsyncBeaverDB,
    catalog: Catalog | None = None,
    *,
    federation_token: str | None = "fed-secret",
):
    from legio.api import create_app

    runtime = Runtime(beaver_db, node_id=NODE_ID)
    return create_app(
        runtime=runtime,
        pattern_catalog=catalog,
        federation_token=federation_token,
    )


async def _client(app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _deposit_result(db: AsyncBeaverDB, tid: str, *, payload: dict | None = None) -> None:
    """Simulate the acceptor's flow having already written the result to the
    agent's shared result queue — the write-before-ack precondition (the
    ``RESULT_DRAIN`` intake still has to collect it into the outbox record)."""
    result = ExecutionResultMessage(
        end_of_level_queue=result_queue_key("cutter"),
        level=1,
        launcher_class="cutter",
        task_id=tid,
        branch_id="synthetic-branch",
        payload=payload or {"cutter": {"result": "cut carve"}},
    )
    await db.queue(queue_key(result_queue_key("cutter"))).put(
        result.model_dump(mode="json"), priority=0.0
    )


async def _seed(runtime: Runtime, tid: str) -> None:
    """Mint the acceptor-side seed for the author's work item (the honest
    modern-triangle path: ``POST /work-items/cutter`` → ``submit_work_item``).

    The seed lets polls resolve the draining agent; without it the outbox
    reads empty (no kick, no error).
    """
    receipt = await runtime.submit_work_item(
        AUTHOR_NODE, (("cutter", "cutter"),), {"raw": "carve"}, task_id=tid
    )
    assert receipt.deposited is True


async def _collect(runtime: Runtime, rounds: int = 10) -> None:
    """Dispatch pending Manager tasks until idle (the node's executor)."""
    for _ in range(rounds):
        if await runtime.manager.run() == 0:
            break


# --------------------------------------------------------------------------
# § non-blocking poll
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outbox_poll_no_result_yet(beaver_db: AsyncBeaverDB) -> None:
    app = build_app(beaver_db, capacity_catalog())
    tid = task_id()
    async with await _client(app) as ac:
        resp = await ac.get(f"/outbox/{tid}", headers=bearer("fed-secret"))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == tid
        assert body["ready"] is False
        assert body["result"] is None


@pytest.mark.asyncio
async def test_outbox_poll_returns_result_when_deposited(beaver_db: AsyncBeaverDB) -> None:
    app = build_app(beaver_db, capacity_catalog())
    runtime = Runtime(beaver_db, node_id=NODE_ID)
    tid = task_id()
    await _seed(runtime, tid)
    await _deposit_result(beaver_db, tid)
    async with await _client(app) as ac:
        await ac.get(f"/outbox/{tid}", headers=bearer("fed-secret"))  # kick-on-miss
        await _collect(runtime)
        resp = await ac.get(f"/outbox/{tid}", headers=bearer("fed-secret"))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ready"] is True
        assert body["result"] == {"cutter": {"result": "cut carve"}}


# --------------------------------------------------------------------------
# § ack consumes; re-read after ack is empty; ack is idempotent
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outbox_ack_consumes_and_re_read_is_empty(beaver_db: AsyncBeaverDB) -> None:
    app = build_app(beaver_db, capacity_catalog())
    runtime = Runtime(beaver_db, node_id=NODE_ID)
    tid = task_id()
    await _seed(runtime, tid)
    await _deposit_result(beaver_db, tid)
    async with await _client(app) as ac:
        await ac.get(f"/outbox/{tid}", headers=bearer("fed-secret"))  # kick-on-miss
        await _collect(runtime)
        acked = await ac.delete(f"/outbox/{tid}", headers=bearer("fed-secret"))
        assert acked.status_code == 200, acked.text
        assert acked.json() == {"id": tid, "acked": True}

        polled = await ac.get(f"/outbox/{tid}", headers=bearer("fed-secret"))
        assert polled.status_code == 200
        assert polled.json()["ready"] is False


@pytest.mark.asyncio
async def test_outbox_ack_on_empty_is_noop(beaver_db: AsyncBeaverDB) -> None:
    app = build_app(beaver_db, capacity_catalog())
    tid = task_id()
    async with await _client(app) as ac:
        acked = await ac.delete(f"/outbox/{tid}", headers=bearer("fed-secret"))
        assert acked.status_code == 200, acked.text
        assert acked.json() == {"id": tid, "acked": False}


# --------------------------------------------------------------------------
# § write-before-ack pin: the result is readable before ANY ack — the ack
#   consumes, it never triggers the write
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outbox_result_is_readable_before_any_ack(beaver_db: AsyncBeaverDB) -> None:
    app = build_app(beaver_db, capacity_catalog())
    runtime = Runtime(beaver_db, node_id=NODE_ID)
    tid = task_id()
    await _seed(runtime, tid)
    await _deposit_result(beaver_db, tid)
    async with await _client(app) as ac:
        await ac.get(f"/outbox/{tid}", headers=bearer("fed-secret"))  # kick-on-miss
        await _collect(runtime)
        first = await ac.get(f"/outbox/{tid}", headers=bearer("fed-secret"))
        assert first.status_code == 200
        assert first.json()["ready"] is True
        # polled again (still no ack) → still ready: the write happened at
        # execution time, independent of any ack
        second = await ac.get(f"/outbox/{tid}", headers=bearer("fed-secret"))
        assert second.json()["ready"] is True


# --------------------------------------------------------------------------
# § idempotency at the queue-message level: duplicate work item (same id) is
#   deposited once, so a single seed is minted and a single root request lands
#   on the class queue (the "executed once" guarantee)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_work_item_never_executed_twice(beaver_db: AsyncBeaverDB) -> None:
    app = build_app(beaver_db, capacity_catalog())
    runtime = Runtime(beaver_db, node_id=NODE_ID)
    tid = task_id()
    item = {
        "task_id": tid,
        "payload": {"raw": "carve"},
        "schema_version": 1000,
    }
    async with await _client(app) as ac:
        first = await ac.post("/work-items/cutter", json=item, headers=bearer("fed-secret"))
        assert first.status_code == 200
        assert first.json()["deposited"] is True

        second = await ac.post("/work-items/cutter", json=item, headers=bearer("fed-secret"))
        assert second.status_code == 200
        assert second.json()["deduplicated"] is True

    # Unwind the acceptor's manager: each pending task is dispatched exactly
    # once. Only one seed exists (the duplicate was refused), so exactly one
    # root ExecutionRequestMessage reaches the class queue.
    dispatched = 0
    for _ in range(4):
        dispatched += await runtime.manager.run()
    assert dispatched >= 1
    queue = beaver_db.queue(queue_key("cutter"))
    assert await queue.count() == 1


# --------------------------------------------------------------------------
# § L1 guard, absent federation surface, invalid task id
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outbox_requires_federation_token(beaver_db: AsyncBeaverDB) -> None:
    app = build_app(beaver_db, capacity_catalog())
    tid = task_id()
    async with await _client(app) as ac:
        missing = await ac.get(f"/outbox/{tid}")
        assert missing.status_code == 401
        wrong = await ac.get(f"/outbox/{tid}", headers=bearer("nope"))
        assert wrong.status_code == 401
        missing_ack = await ac.delete(f"/outbox/{tid}")
        assert missing_ack.status_code == 401


@pytest.mark.asyncio
async def test_outbox_absent_without_federation_config(beaver_db: AsyncBeaverDB) -> None:
    app = build_app(beaver_db, capacity_catalog(), federation_token=None)
    tid = task_id()
    async with await _client(app) as ac:
        assert (await ac.get(f"/outbox/{tid}")).status_code == 404


@pytest.mark.asyncio
async def test_outbox_rejects_invalid_task_id(beaver_db: AsyncBeaverDB) -> None:
    app = build_app(beaver_db, capacity_catalog())
    async with await _client(app) as ac:
        resp = await ac.get("/outbox/not-a-task-id", headers=bearer("fed-secret"))
        assert resp.status_code == 422
        assert resp.json()["code"] == "invalid_request"
        ack = await ac.delete("/outbox/not-a-task-id", headers=bearer("fed-secret"))
        assert ack.status_code == 422
