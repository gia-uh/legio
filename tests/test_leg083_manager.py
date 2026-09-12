"""Contract tests for LEG-083 — the Manager generic task environment (§6.1).

These tests define the public contract for the domain-free task engine that
the docs call the ``Manager`` (``legio.manager`` module and ``Manager`` class):
generic submit/status/pause/resume/cancel over the ``tasks`` /
``pending_tasks`` / ``control`` beaver scopes, an in-process callable registry
and a polling task executor. Written red first against the designed surface.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any, cast

import pytest
from beaver import AsyncBeaverDB

from legio.errors import InvalidNameError
from legio.manager import Manager, TaskRecord, TaskStatus
from legio.naming import validate_task_id

NODE_A = "mgr-a@host1"
NODE_B = "mgr-b@host2"


def _manager(db, node_id: str = NODE_A) -> Manager:
    return Manager(db, node_id=node_id)


# --- construction / task id minting -----------------------------------------


def test_manager_rejects_malformed_node_id(beaver_db) -> None:
    with pytest.raises(InvalidNameError):
        Manager(beaver_db, node_id="local")


def test_manager_requires_a_connected_beaver_db() -> None:
    with pytest.raises(TypeError):
        Manager(cast(AsyncBeaverDB, None), node_id=NODE_A)


@pytest.mark.asyncio
async def test_submit_task_mints_node_uuid_task_id(beaver_db) -> None:
    manager = _manager(beaver_db)
    task_id = await manager.submit_task("build")
    validate_task_id(task_id)
    assert task_id.startswith(f"{NODE_A}:")


@pytest.mark.asyncio
async def test_task_record_has_no_instance_id_identity(beaver_db) -> None:
    manager = _manager(beaver_db)
    task_id = await manager.submit_task("build")
    record = await manager.status(task_id)
    assert record is not None
    assert record.task_id == task_id
    assert not hasattr(record, "instance_id")


# --- submit / status record fields ------------------------------------------


@pytest.mark.asyncio
async def test_submit_records_pending_record_with_fields(beaver_db) -> None:
    manager = _manager(beaver_db)
    task_id = await manager.submit_task("build", 3, key="value")
    record = await manager.status(task_id)
    assert record is not None
    assert record.name == "build"
    assert record.status == TaskStatus.PENDING
    assert record.args == [3]
    assert record.kwargs == {"key": "value"}
    assert record.enqueued_at is not None
    assert record.started_at is None
    assert record.finished_at is None
    assert record.result is None
    assert record.error is None


@pytest.mark.asyncio
async def test_status_of_unknown_task_returns_none(beaver_db) -> None:
    manager = _manager(beaver_db)
    assert await manager.status("mgr-a@host1:00000000-0000-0000-0000-000000000000") is None


# --- scope footprint ---------------------------------------------------------


@pytest.mark.asyncio
async def test_scope_footprint_is_exactly_tasks_pending_tasks_control(beaver_db) -> None:
    manager = _manager(beaver_db)
    task_id = await manager.submit_task("build")
    await manager.pause(task_id)

    task_record = await beaver_db.dict("tasks").fetch(task_id)
    assert task_record is not None
    assert task_record["name"] == "build"

    pending_count = await beaver_db.queue("pending_tasks").count()
    assert pending_count == 1

    control = await beaver_db.dict("control").fetch(task_id)
    assert control is not None
    assert control["mode"] == "pause"


# --- executor: run() ---------------------------------------------------------


@pytest.mark.asyncio
async def test_run_with_nothing_due_returns_zero(beaver_db) -> None:
    manager = _manager(beaver_db)
    assert await manager.run() == 0


@pytest.mark.asyncio
async def test_successful_callable_lifecycle(beaver_db) -> None:
    manager = _manager(beaver_db)

    async def add(left: int, right: int) -> int:
        return left + right

    manager.register("add", add)
    task_id = await manager.submit_task("add", 2, right=3)
    assert await manager.run() == 1

    record = await manager.status(task_id)
    assert record is not None
    assert record.status == TaskStatus.SUCCESS
    assert record.result == 5
    assert record.error is None
    assert record.started_at is not None
    assert record.finished_at is not None
    assert record.enqueued_at <= record.started_at <= record.finished_at


@pytest.mark.asyncio
async def test_failed_callable_records_error_visibly(beaver_db, caplog) -> None:
    manager = _manager(beaver_db)

    async def explode() -> None:
        raise ValueError("task boom")

    manager.register("explode", explode)
    task_id = await manager.submit_task("explode")
    with caplog.at_level(logging.WARNING, logger="legio.manager"):
        assert await manager.run() == 1

    record = await manager.status(task_id)
    assert record is not None
    assert record.status == TaskStatus.FAILED
    assert "task boom" in (record.error or "")
    assert record.finished_at is not None
    assert any("failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_unregistered_callable_fails_visibly_at_dispatch(beaver_db) -> None:
    manager = _manager(beaver_db)
    task_id = await manager.submit_task("missing")
    assert await manager.run() == 1
    record = await manager.status(task_id)
    assert record is not None
    assert record.status == TaskStatus.FAILED
    assert "unregistered" in (record.error or "").lower()


# --- single execution guarantees ---------------------------------------------


@pytest.mark.asyncio
async def test_task_executes_exactly_once_across_two_executors(beaver_db) -> None:
    manager_a = _manager(beaver_db)
    manager_b = _manager(beaver_db, node_id=NODE_B)
    calls: list[str] = []

    async def build(who: str) -> str:
        calls.append(who)
        return who

    manager_a.register("build", build)
    manager_b.register("build", build)

    task_id_a = await manager_a.submit_task("build", "a")
    task_id_b = await manager_b.submit_task("build", "b")

    assert await manager_a.run() == 1
    assert await manager_b.run() == 1
    assert await manager_a.run() == 0
    assert await manager_b.run() == 0

    assert calls == ["a", "b"]
    record_a = await manager_a.status(task_id_a)
    record_b = await manager_a.status(task_id_b)
    assert record_a is not None and record_a.status == TaskStatus.SUCCESS
    assert record_b is not None and record_b.status == TaskStatus.SUCCESS


@pytest.mark.asyncio
async def test_a_single_task_runs_once_across_executors(beaver_db) -> None:
    manager_a = _manager(beaver_db)
    manager_b = _manager(beaver_db, node_id=NODE_B)
    calls = 0

    async def once() -> None:
        nonlocal calls
        calls += 1

    manager_a.register("once", once)
    manager_b.register("once", once)
    await manager_a.submit_task("once")

    assert await manager_a.run() == 1
    assert await manager_b.run() == 0
    assert calls == 1


@pytest.mark.asyncio
async def test_crashed_executor_task_is_surfaced_not_rerun(beaver_db) -> None:
    manager = _manager(beaver_db)
    task_id = f"{NODE_A}:00000000-0000-0000-0000-000000000001"
    crashed = TaskRecord(
        task_id=task_id,
        name="build",
        status=TaskStatus.RUNNING,
        enqueued_at="1985-01-01T00:00:00+00:00",
        started_at="1985-01-01T00:00:00+00:00",
    )
    await beaver_db.dict("tasks").set(task_id, crashed.model_dump(mode="json"))

    assert await manager.run() == 0
    record = await manager.status(task_id)
    assert record is not None
    assert record.status == TaskStatus.RUNNING


# --- cooperative control: pause / resume -------------------------------------


@pytest.mark.asyncio
async def test_pause_pending_then_resume_keeps_non_terminal(beaver_db) -> None:
    manager = _manager(beaver_db)
    runs = 0

    async def build() -> str:
        nonlocal runs
        runs += 1
        return "ok"

    manager.register("build", build)
    task_id = await manager.submit_task("build")

    await manager.pause(task_id)
    assert await manager.run() == 1
    record = await manager.status(task_id)
    assert record is not None
    assert record.status == TaskStatus.PENDING
    assert runs == 0

    await manager.resume(task_id)
    assert await manager.run() == 1
    record = await manager.status(task_id)
    assert record is not None
    assert record.status == TaskStatus.SUCCESS
    assert record.result == "ok"
    assert runs == 1


@pytest.mark.asyncio
async def test_cancellable_generator_pause_then_resume_mid_run(beaver_db) -> None:
    manager = _manager(beaver_db)
    events: list[str] = []
    gate = asyncio.Event()

    async def sequenced() -> AsyncIterator[str]:
        events.append("enter")
        yield "first"
        await gate.wait()
        yield "second"

    manager.register("sequenced", sequenced)
    task_id = await manager.submit_task("sequenced")

    runner = asyncio.create_task(manager.run())
    while "enter" not in events:
        await asyncio.sleep(0.001)
    await manager.pause(task_id)
    gate.set()
    await runner

    record = await manager.status(task_id)
    assert record is not None
    assert record.status == TaskStatus.RUNNING
    assert record.finished_at is None

    await manager.resume(task_id)
    assert await manager.run() == 1
    record = await manager.status(task_id)
    assert record is not None
    assert record.status == TaskStatus.SUCCESS
    assert record.result == "second"


@pytest.mark.asyncio
async def test_control_verbs_on_unknown_task_raise(beaver_db) -> None:
    manager = _manager(beaver_db)
    unknown = f"{NODE_A}:00000000-0000-0000-0000-000000000002"
    for verb in ("pause", "resume", "cancel"):
        with pytest.raises(KeyError):
            await getattr(manager, verb)(unknown)


# --- cooperative control: control mode read -----------------------------------


@pytest.mark.asyncio
async def test_control_mode_is_none_before_any_control_instruction(beaver_db) -> None:
    manager = _manager(beaver_db)
    task_id = await manager.submit_task("build")
    assert await manager.control_mode(task_id) is None


@pytest.mark.asyncio
async def test_control_mode_is_none_for_unknown_task(beaver_db) -> None:
    manager = _manager(beaver_db)
    unknown = f"{NODE_A}:00000000-0000-0000-0000-000000000002"
    assert await manager.control_mode(unknown) is None


@pytest.mark.asyncio
async def test_control_mode_reflects_pause_resume_cancel(beaver_db) -> None:
    manager = _manager(beaver_db)
    task_id = await manager.submit_task("build")
    await manager.pause(task_id)
    assert await manager.control_mode(task_id) == "pause"
    await manager.resume(task_id)
    assert await manager.control_mode(task_id) == "run"
    await manager.cancel(task_id)
    assert await manager.control_mode(task_id) == "cancel"


# --- cooperative control: cancel ---------------------------------------------


@pytest.mark.asyncio
async def test_cancel_pending_task_never_invokes_callable(beaver_db) -> None:
    manager = _manager(beaver_db)
    invoked = 0

    async def build() -> None:
        nonlocal invoked
        invoked += 1

    manager.register("build", build)
    task_id = await manager.submit_task("build")

    await manager.cancel(task_id)
    assert await manager.run() == 1

    record = await manager.status(task_id)
    assert record is not None
    assert record.status == TaskStatus.FAILED
    assert record.error == "cancelled"
    assert invoked == 0


@pytest.mark.asyncio
async def test_cancel_parked_generator_is_terminal_and_generates_cancelling(beaver_db) -> None:
    manager = _manager(beaver_db)
    events: list[str] = []
    gate = asyncio.Event()

    async def tracked() -> AsyncIterator[str]:
        try:
            events.append("enter")
            yield "first"
            await gate.wait()
            yield "second"
        finally:
            events.append("closed")

    manager.register("tracked", tracked)
    task_id = await manager.submit_task("tracked")

    runner = asyncio.create_task(manager.run())
    while "enter" not in events:
        await asyncio.sleep(0.001)
    await manager.cancel(task_id)
    gate.set()
    await runner

    record = await manager.status(task_id)
    assert record is not None
    assert record.status == TaskStatus.FAILED
    assert record.error == "cancelled"
    assert "closed" in events


# --- registration contract ---------------------------------------------------


@pytest.mark.asyncio
async def test_register_rejects_non_async_callables(beaver_db) -> None:
    manager = _manager(beaver_db)

    def sync_callable() -> None:
        return None

    with pytest.raises(TypeError):
        manager.register("sync", sync_callable)
    with pytest.raises(TypeError):
        manager.register("", sync_callable)


@pytest.mark.asyncio
async def test_register_accepts_async_fn_and_async_generator(
    beaver_db,
) -> tuple[Callable[..., Any], Callable[..., Any]]:
    manager = _manager(beaver_db)

    async def plain() -> int:
        return 1

    async def streamed() -> AsyncIterator[int]:
        yield 1

    manager.register("plain", plain)
    manager.register("streamed", streamed)
    assert inspect.isasyncgenfunction(streamed)
    return plain, streamed


@pytest.mark.asyncio
async def test_manager_is_polling_only_never_blocking(beaver_db) -> None:
    manager = _manager(beaver_db)
    done = await manager.run()
    assert done == 0
