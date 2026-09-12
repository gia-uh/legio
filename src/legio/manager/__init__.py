"""`legio.manager` — the generic, domain-free `Manager` task engine (§6.1).

The ``Manager`` (LEG-083, AGENT_LIFECYCLE §6.1) is the runtime triangle's one
task engine: it manages *any kind of task* (submit, status, pause, resume,
cancel) over its own beaver scopes (``tasks`` dict, ``pending_tasks`` queue,
``control`` dict), runs registered callables through a polling executor
(generator callables are cooperative: control is honored at each yield), and
never knows what a callable means (rule 7).

The business ``submit``/``status`` surface lives on the ``Runtime``
(``legio.runtime``, the public face — LEG-085), which rides ``Manager.submit_task``
for its facts; this module hosts only the generic engine, the ``TaskStatus``
enum and the ``TaskRecord``. It is async and polling-only (rule 8): it never
blocks or sleeps, and there is no scheduling queue or ``next_run_at``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from beaver import AsyncBeaverDB
from pydantic import BaseModel

from legio.naming import validate_node_id, validate_task_id

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    """Lifecycle status of a generic Manager task (§6.1)."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLING = "cancelling"


class TaskRecord(BaseModel):
    """The domain-free record of a generic Manager task (§6.1).

    A task is a ``name`` + a ``task_id`` + ``args/kwargs`` + a status +
    result/error + timestamps. ``cancelling`` is the visible half-open state of
    a cooperative cancel. It never carries an ``instance_id``.
    """

    task_id: str
    name: str
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    status: TaskStatus = TaskStatus.PENDING
    enqueued_at: str
    started_at: str | None = None
    finished_at: str | None = None
    result: Any | None = None
    error: str | None = None


class Manager:
    """The generic, domain-free task environment (AGENT_LIFECYCLE §6.1).

    ``Manager`` is the runtime triangle's one task engine over native beaver:
    it manages any kind of task (submit, status, pause, resume, cancel), runs
    registered callables through a polling executor, and never knows what a
    callable means (rule 7). Callables are registered **in-process** (they are
    the task executor's execution scope, never a registry — rule 13); the
    authority of what happened is always the persistent beaver task record.

    Beaver footprint (Manager-owned scopes): dict ``tasks`` (``task_id`` →
    ``TaskRecord``), queue ``pending_tasks`` (task ids due now, priority 0),
    dict ``control`` (cooperative control per task with TTL). Results/errors
    travel inside the task record — there is no result queue, no scheduling
    queue and no ``next_run_at`` (rule 8).

    The task executor is polling-only: ``run()`` pops the next ``pending_tasks``
    id (destructive/atomic — a task executes once, never re-run) and dispatches
    it. Generator callables are driven step by step, honoring ``control`` at
    each yield: run → advance, pause → park in-process (non-terminal), cancel →
    ``cancelling`` → ``failed(cancelled)``. Plain (async fn) callables are
    awaited once.

    ``Manager`` is the runtime triangle's one task engine: the business
    ``submit``/``status`` surface lives on the ``Runtime`` (LEG-085), which rides
    ``Manager.submit_task`` for its facts. The manager only executes generic
    tasks and never sees the flow layer.
    """

    _TASKS_SCOPE = "tasks"
    _PENDING_SCOPE = "pending_tasks"
    _CONTROL_SCOPE = "control"

    def __init__(self, db: AsyncBeaverDB, *, node_id: str, control_ttl: float = 60.0) -> None:
        if db is None:
            raise TypeError("Manager requires a connected AsyncBeaverDB (beaver system substrate)")
        validate_node_id(node_id)
        self._db = db
        self._node_id = node_id
        self._control_ttl = control_ttl
        self._registry: dict[str, Callable[..., Any]] = {}
        self._parked: dict[str, AsyncGenerator[Any, Any]] = {}
        self._last_yields: dict[str, Any] = {}
        self._tasks = db.dict(self._TASKS_SCOPE)
        self._pending = db.queue(self._PENDING_SCOPE)
        self._control = db.dict(self._CONTROL_SCOPE)
        logger.info("manager executor up node=%s scopes=tasks,pending_tasks,control", node_id)

    def register(self, name: str, callable_: Callable[..., Any]) -> None:
        """Register a callable in-process (async fn or async generator)."""
        if not name:
            raise TypeError("task name must be a non-empty string")
        if not inspect.iscoroutinefunction(callable_) and not inspect.isasyncgenfunction(callable_):
            raise TypeError(f"callable {name!r} must be an async function or async generator")
        self._registry[name] = callable_
        logger.info("manager register name=%s", name)

    async def submit_task(
        self, name: str, *args: Any, task_id: str | None = None, **kwargs: Any
    ) -> str:
        """Submit a generic task; returns its ``task_id`` (``<node_id>:<uuid>``).

        An explicit ``task_id`` (keyword-only) lets a caller — the Runtime
        submitting the business seed and the bring-up facts per §7.1 — mint the
        id itself and have the Manager record and own it. When ``None`` the
        Manager mints `<node_id>:<uuid>` as usual. A ``task_id`` that already
        exists is rejected visibly (rule 9: never overwrite a live record).
        """
        if task_id is None:
            task_id = f"{self._node_id}:{uuid.uuid4()}"
        else:
            validate_task_id(task_id)
            if await self._tasks.fetch(task_id) is not None:
                logger.warning("manager submit deny task=%s (already exists)", task_id)
                raise ValueError(f"task {task_id!r} already exists")
        record = TaskRecord(
            task_id=task_id,
            name=name,
            args=list(args),
            kwargs=kwargs,
            status=TaskStatus.PENDING,
            enqueued_at=_utc_now_iso(),
        )
        await self._tasks.set(task_id, record.model_dump(mode="json"))
        await self._pending.put(task_id, priority=0.0)
        logger.info("manager submit task=%s name=%s", task_id, name)
        return task_id

    async def status(self, task_id: str) -> TaskRecord | None:
        """Return the task record, or ``None`` if no such task exists."""
        data = await self._tasks.fetch(task_id)
        if data is None:
            return None
        return TaskRecord.model_validate(data)

    async def control_mode(self, task_id: str) -> str | None:
        """Return the stored cooperative control mode (``run``/``pause``/
        ``cancel``) for a task, or ``None`` when no explicit control
        instruction is set (equivalent to ``run``)."""
        control = await self._control.fetch(task_id)
        if control is None:
            return None
        return control.get("mode")

    async def pause(self, task_id: str) -> None:
        """Cooperative non-terminal suspension (disable ≠ destroy, rule 8)."""
        await self._require_task(task_id)
        await self._set_control(task_id, "pause")
        logger.info("manager pause task=%s", task_id)

    async def resume(self, task_id: str) -> None:
        """Release a paused task back to ``run`` (non-terminal)."""
        record = await self._require_task(task_id)
        await self._set_control(task_id, "run")
        if record.status == TaskStatus.PENDING or task_id in self._parked:
            await self._pending.put(task_id, priority=0.0)
        logger.info("manager resume task=%s", task_id)

    async def cancel(self, task_id: str) -> None:
        """Request a cooperative, terminal cancel."""
        record = await self._require_task(task_id)
        await self._set_control(task_id, "cancel")
        if record.status == TaskStatus.PENDING or task_id in self._parked:
            await self._pending.put(task_id, priority=0.0)
        logger.info("manager cancel task=%s", task_id)

    async def run(self) -> int:
        """One polling pass: pop the next pending task and dispatch it.

        Returns the number of tasks dispatched (0 when nothing is due).
        ``get`` is destructive/atomic, so each task executes exactly once and a
        crashed executor is surfaced visibly, never silently re-run.
        """
        try:
            item = await self._pending.get(block=False)
        except IndexError:
            return 0
        task_id = item.data
        await self._dispatch(task_id)
        return 1

    async def _dispatch(self, task_id: str) -> None:
        data = await self._tasks.fetch(task_id)
        if data is None:
            logger.warning("manager dispatch unknown task=%s", task_id)
            return
        record = TaskRecord.model_validate(data)

        if record.status == TaskStatus.CANCELLING:
            logger.warning("manager dispatch half-open cancelled task=%s", task_id)
            return

        if task_id in self._parked:
            await self._drive_parked(task_id, record)
            return

        if record.status == TaskStatus.RUNNING:
            logger.warning("manager skip running task=%s (visible, not re-run)", task_id)
            return

        canceling = await self._control_mode(task_id) == "cancel"
        if canceling:
            record.status = TaskStatus.CANCELLING
            await self._tasks.set(task_id, record.model_dump(mode="json"))
            record.status = TaskStatus.FAILED
            record.error = "cancelled"
            record.finished_at = _utc_now_iso()
            await self._tasks.set(task_id, record.model_dump(mode="json"))
            logger.warning("manager cancelled pending task=%s", task_id)
            return

        pausing = await self._control_mode(task_id) == "pause"
        if pausing and record.status == TaskStatus.PENDING:
            logger.info("manager paused pending task=%s", task_id)
            return

        callable_ = self._registry.get(record.name)
        if callable_ is None:
            record.status = TaskStatus.FAILED
            record.error = f"unregistered task name {record.name!r}"
            record.finished_at = _utc_now_iso()
            await self._tasks.set(task_id, record.model_dump(mode="json"))
            logger.error("manager unregistered name=%s task=%s", record.name, task_id)
            return

        record.status = TaskStatus.RUNNING
        record.started_at = _utc_now_iso()
        await self._tasks.set(task_id, record.model_dump(mode="json"))
        logger.info("manager running task=%s name=%s", task_id, record.name)

        if inspect.isasyncgenfunction(callable_):
            self._parked[task_id] = callable_(*record.args, **record.kwargs)
            await self._drive_parked(task_id, record)
        else:
            await self._await_plain(task_id, record, callable_)

    async def _await_plain(self, task_id: str, record: TaskRecord, callable_) -> None:
        try:
            result = await callable_(*record.args, **record.kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            record.status = TaskStatus.FAILED
            record.error = str(exc)
            record.finished_at = _utc_now_iso()
            await self._tasks.set(task_id, record.model_dump(mode="json"))
            logger.exception("manager failed task=%s name=%s", task_id, record.name)
            return
        record.status = TaskStatus.SUCCESS
        record.result = result
        record.finished_at = _utc_now_iso()
        await self._tasks.set(task_id, record.model_dump(mode="json"))
        logger.info("manager success task=%s name=%s result=%r", task_id, record.name, result)

    async def _drive_parked(self, task_id: str, record: TaskRecord) -> None:
        generator = self._parked[task_id]
        last_yielded = self._last_yields.get(task_id)
        while True:
            mode = await self._control_mode(task_id)
            if mode == "cancel":
                record.status = TaskStatus.CANCELLING
                await self._tasks.set(task_id, record.model_dump(mode="json"))
                await generator.aclose()
                self._parked.pop(task_id, None)
                self._last_yields.pop(task_id, None)
                record.status = TaskStatus.FAILED
                record.error = "cancelled"
                record.finished_at = _utc_now_iso()
                await self._tasks.set(task_id, record.model_dump(mode="json"))
                logger.warning("manager cancelled task=%s name=%s", task_id, record.name)
                return
            if mode == "pause":
                logger.info("manager parked task=%s name=%s", task_id, record.name)
                return
            try:
                last_yielded = await generator.__anext__()
                self._last_yields[task_id] = last_yielded
            except StopAsyncIteration:
                self._parked.pop(task_id, None)
                result = self._last_yields.pop(task_id, None)
                record.status = TaskStatus.SUCCESS
                record.result = result
                record.finished_at = _utc_now_iso()
                await self._tasks.set(task_id, record.model_dump(mode="json"))
                logger.info(
                    "manager success task=%s name=%s result=%r",
                    task_id,
                    record.name,
                    result,
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await generator.aclose()
                self._parked.pop(task_id, None)
                self._last_yields.pop(task_id, None)
                record.status = TaskStatus.FAILED
                record.error = str(exc)
                record.finished_at = _utc_now_iso()
                await self._tasks.set(task_id, record.model_dump(mode="json"))
                logger.exception("manager failed task=%s name=%s", task_id, record.name)
                return

    async def _require_task(self, task_id: str) -> TaskRecord:
        record = await self.status(task_id)
        if record is None:
            logger.warning("manager control unknown task=%s", task_id)
            raise KeyError(f"unknown task {task_id!r}")
        return record

    async def _set_control(self, task_id: str, mode: str) -> None:
        await self._control.set(task_id, {"mode": mode}, ttl_seconds=self._control_ttl)

    async def _control_mode(self, task_id: str) -> str:
        control = await self._control.fetch(task_id)
        if control is None:
            return "run"
        return control.get("mode", "run")


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["AsyncBeaverDB", "Manager", "TaskRecord", "TaskStatus"]
