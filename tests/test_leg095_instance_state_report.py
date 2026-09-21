"""Contract tests for LEG-095 — the instance state report (control out-side).

The control channel's ``read → process → write`` contract was missing its write:
a disabled instance's ``_honor_control`` deposited nothing, so the Runtime wrote
the Registry **optimistic, a posteriori** (LEG-087 "no ack by design"). This file
proves the LEG-095 model: after honoring a control, the agent deposits an
unsigned ``AgentStateReport`` (instance, action, seq, resulting state) on a
node-internal intake queue; the Runtime's ``STATE_REPORT`` fact validates it by
correlation with its own **pending mint ledger** and only then applies Registry
state — and the verb confirms only when the Registry reads the target state
(report-convergent, never optimistic).

Red first: the report algebra, the agent's deposit per honored action, the
intake's ledger-correlated validation (orphans are visible WARNINGs, never
applied), and the verb confirms (missing report → visible ``RecoverableError``,
destroy stays structural).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Self

import pytest
from beaver import AsyncBeaverDB

from legio.agents.base import AgentBase
from legio.config import LifecycleConfig, LifecycleParams
from legio.errors import RecoverableError
from legio.flow import (
    CONTROL_PRIORITY,
    STATE_REPORT_SCOPE,
    AgentStateReport,
    ControlAction,
    ControlMessage,
    ControlOrigin,
    ControlVerifier,
    ExecutionRequestMessage,
    ReportedState,
    sign_control,
)
from legio.manager import TaskStatus
from legio.naming import queue_key
from legio.patterns import load_patterns
from legio.registry import ActivityState
from legio.runtime import Runtime
from legio.tools import AvailableToolsRegistry

NODE_ID = "runtime-a@host1"
KEY = bytes(range(32))

# --- §A fixtures: the report envelope ----------------------------------------


def test_agent_state_report_is_strict_and_unsigned() -> None:
    """§A: the report is frozen, extra-forbidden, and carries exactly the
    instance/action/seq (correlation key) + resulting state — no signature (the
    agent holds only a verify-only handle and can never mint)."""
    report = AgentStateReport(
        instance_id="main-1", action=ControlAction.DISABLE, seq=7, state=ReportedState.PARKED
    )
    assert report.message_type == "state_report"
    assert report.schema_version >= 1
    with pytest.raises(ValueError):  # extra field forbidden
        AgentStateReport.model_validate({**report.model_dump(), "x": 1})


# --- §B: the agent deposits exactly one report per honored control ------------


def make_request(*, task_id: str) -> ExecutionRequestMessage:
    return ExecutionRequestMessage(
        level_route=(("main", "in"),),
        current_index=0,
        end_of_level_queue="client",
        level=1,
        task_id=task_id,
        payload={"in": {"v": 1}},
    )


class EchoAgent(AgentBase):
    """A minimal atomic agent that records every processed work item."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(output_as="out", **kwargs)
        self.processed: list[ExecutionRequestMessage] = []

    async def _handle(self, request: ExecutionRequestMessage) -> dict[str, Any]:
        self.processed.append(request)
        return {"out": request.payload}


class RunningLoop:
    """A context-managed standing loop task; always torn down on exit."""

    def __init__(self, agent: EchoAgent, instance_id: str) -> None:
        self.agent = agent
        self.instance_id = instance_id
        self.task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        self.task = asyncio.create_task(self.agent.standing_loop(self.instance_id))
        await asyncio.sleep(0.0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


async def queue_count(db: AsyncBeaverDB, queue: str) -> int:
    return await db.queue(queue_key(queue)).count()


async def report_count(db: AsyncBeaverDB) -> int:
    return await db.queue(STATE_REPORT_SCOPE).count()


async def drain_reports(db: AsyncBeaverDB) -> list[AgentStateReport]:
    """Destructively drain the state-report intake."""
    queue = db.queue(STATE_REPORT_SCOPE)
    reports: list[AgentStateReport] = []
    while True:
        item = await queue.peek()
        if item is None:
            return reports
        await queue.get(block=False)
        reports.append(AgentStateReport.model_validate(item.data))


async def consume_until(predicate: Any, *, timeout: float = 3.0) -> None:
    """Repeatedly evaluate ``predicate`` until truthy (test-side clock wait)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"consume_until timed out after {timeout}s")
        await asyncio.sleep(0.05)


async def deposit_control(
    db: AsyncBeaverDB,
    message: ControlMessage,
    *,
    priority: float = CONTROL_PRIORITY,
) -> None:
    await db.queue(queue_key("main")).put(message.model_dump(mode="json"), priority=priority)


def put_control(
    action: ControlAction,
    seq: int,
    target: str = "main-1",
) -> ControlMessage:
    return sign_control(
        KEY, target_instance=target, action=action, seq=seq, origin=ControlOrigin.OPERATOR
    )


@pytest.mark.asyncio
async def test_disable_honor_deposits_one_parked_report(beaver_db: AsyncBeaverDB) -> None:
    """§B: honoring a ``disable`` deposits exactly one report (parked) with the
    honored seq on the intake; nothing else."""
    agent = EchoAgent(
        agent_id="main",
        db=beaver_db,
        control_verifier=ControlVerifier(KEY),
    )
    async with RunningLoop(agent, "main-1"):
        await deposit_control(beaver_db, put_control(ControlAction.DISABLE, seq=1))
        await consume_until(lambda: agent._control_paused)
        reports = await drain_reports(beaver_db)
    assert reports == [
        AgentStateReport(
            instance_id="main-1",
            action=ControlAction.DISABLE,
            seq=1,
            state=ReportedState.PARKED,
        )
    ]


@pytest.mark.asyncio
async def test_enable_honor_deposits_one_ready_report(beaver_db: AsyncBeaverDB) -> None:
    """§B: disable → parked (seq 1), enable → ready (seq 2); one report each."""
    agent = EchoAgent(
        agent_id="main",
        db=beaver_db,
        control_verifier=ControlVerifier(KEY),
    )
    async with RunningLoop(agent, "main-1"):
        await deposit_control(beaver_db, put_control(ControlAction.DISABLE, seq=1))
        await consume_until(lambda: agent._control_paused)
        await deposit_control(beaver_db, put_control(ControlAction.ENABLE, seq=2))
        await consume_until(lambda: not agent._control_paused)
        reports = await drain_reports(beaver_db)
    assert [r.state for r in reports] == [ReportedState.PARKED, ReportedState.READY]
    assert [r.seq for r in reports] == [1, 2]


@pytest.mark.asyncio
async def test_terminate_honor_deposits_one_terminating_report(
    beaver_db: AsyncBeaverDB,
) -> None:
    """§B: terminate_with_drain deposits a terminating report before the loop
    returns (informational for the Runtime)."""
    agent = EchoAgent(
        agent_id="main",
        db=beaver_db,
        control_verifier=ControlVerifier(KEY),
    )
    loop = RunningLoop(agent, "main-1")
    await loop.__aenter__()
    try:
        await deposit_control(beaver_db, put_control(ControlAction.TERMINATE_WITH_DRAIN, seq=1))
        await asyncio.wait_for(loop.task, timeout=3.0)  # type: ignore[arg-type]
        reports = await drain_reports(beaver_db)
    finally:
        await loop.__aexit__()
    assert reports == [
        AgentStateReport(
            instance_id="main-1",
            action=ControlAction.TERMINATE_WITH_DRAIN,
            seq=1,
            state=ReportedState.TERMINATING,
        )
    ]


@pytest.mark.asyncio
async def test_rejected_controls_emit_no_report(beaver_db: AsyncBeaverDB) -> None:
    """§B/rule 9: a rejected control (bad signature, replay) is dropped and
    emits nothing — only honored controls report."""
    agent = EchoAgent(
        agent_id="main",
        db=beaver_db,
        control_verifier=ControlVerifier(KEY),
    )
    async with RunningLoop(agent, "main-1"):
        forged = put_control(ControlAction.DISABLE, seq=2).model_dump()
        forged["seq"] = 9
        await beaver_db.queue(queue_key("main")).put(forged, priority=CONTROL_PRIORITY)
        agent._last_control_seq = 1  # a prior control was already honored
        replayed = put_control(ControlAction.DISABLE, seq=1)
        await deposit_control(beaver_db, replayed)
        await asyncio.sleep(0.2)
        assert not agent._control_paused
        assert await report_count(beaver_db) == 0
        # a genuinely fresh sequence is still honored and reports
        await deposit_control(beaver_db, put_control(ControlAction.DISABLE, seq=2))
        await consume_until(lambda: agent._control_paused)
        reports = await drain_reports(beaver_db)
    assert [r.seq for r in reports] == [2]


@pytest.mark.asyncio
async def test_foreign_target_control_emits_no_report(beaver_db: AsyncBeaverDB) -> None:
    """§B: a control addressed to another instance is requeued, never honored
    and never reported by this instance."""
    agent = EchoAgent(
        agent_id="main",
        db=beaver_db,
        control_verifier=ControlVerifier(KEY),
    )
    async with RunningLoop(agent, "main-1"):
        await deposit_control(beaver_db, put_control(ControlAction.DISABLE, seq=1, target="main-2"))
        await asyncio.sleep(0.2)
        assert not agent._control_paused
        assert await report_count(beaver_db) == 0


# --- §C: runtime intake (pending-ledger correlation) + report-convergent verbs --


def echo_text(text: str) -> dict:
    """A fictitious domain-free tool."""
    return {"text": text}


def _atomic_yaml(name: str) -> str:
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
      text: {{type: string}}
output:
  output_as: {name}
  output_type: json
  output_schema:
    type: object
    properties:
      text: {{type: string}}
tool: {name}
parameters:
  text: "{{{name}.text}}"
"""


def _catalog_spec(name: str):
    catalog = load_patterns(_atomic_yaml(name))
    spec = catalog.specs[name]
    return spec.name, spec


def _runtime(db: AsyncBeaverDB, *, lifecycle: LifecycleConfig | None = None) -> Runtime:
    return Runtime(db, node_id=NODE_ID, control_key=KEY, lifecycle=lifecycle)


def _tool_agent(db: AsyncBeaverDB) -> AgentBase:
    from legio.agents.tool_agent import ToolAgent

    registry = AvailableToolsRegistry()
    registry.declare(
        "pinger",
        implementation="tests.test_leg095_instance_state_report.echo_text",
        policy={"timeout": 30, "retries": 0},
    )
    return ToolAgent(
        agent_id="pinger",
        db=db,
        available_tools=registry,
        tool_name="pinger",
        parameters={"text": "{pinger.text}"},
        control_verifier=ControlVerifier(KEY),
    )


def _start_executor(runtime: Runtime, *, pumps: int = 3) -> list[asyncio.Task]:
    async def _loop() -> None:
        while True:
            await runtime.manager.run()
            await asyncio.sleep(0)

    return [asyncio.create_task(_loop()) for _ in range(pumps)]


async def _teardown(
    db: AsyncBeaverDB,
    runtime: Runtime,
    names: list[str],
    *,
    pumps: list[asyncio.Task] | None = None,
) -> None:
    for name in reversed(names):
        try:
            await runtime.destroy_class(name, mode="now")
        except Exception as exc:  # noqa: BLE001 — teardown is best effort
            print(f"teardown skipped class={name}: {exc}")
    for pump in pumps or []:
        pump.cancel()
    if pumps:
        await asyncio.gather(*pumps, return_exceptions=True)


async def _instance_state(
    runtime: Runtime, class_name: str, instance_id: str
) -> ActivityState | None:
    record = await runtime.registry.get_instance(class_name, instance_id)
    return None if record is None else record.state


@pytest.mark.asyncio
async def test_disable_confirms_only_after_report_consumed(beaver_db: AsyncBeaverDB) -> None:
    """§C: ``disable_instance`` returns only when the intake consumed the report
    (ledger hit) and the Registry reads disabled — never before. Single writer
    to the instance state is the intake; the verb never writes postieriopti."""
    runtime = _runtime(beaver_db)
    runtime.mount_agents({"pinger": _tool_agent(beaver_db)})
    pumps = _start_executor(runtime)
    name, spec = _catalog_spec("pinger")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("pinger"), pool=1)
        instance_id = "pinger-1"
        task_id = runtime._instance_tasks[(name, instance_id)]

        await runtime.disable_instance(name, instance_id)

        assert await _instance_state(runtime, name, instance_id) == ActivityState.DISABLED
        assert runtime._pending_controls == {}  # ledger consumed by the report
        assert await runtime._state_reports.count() == 0  # intake drained
        record = await runtime.manager.status(task_id)
        assert record is not None and record.status == TaskStatus.RUNNING
    finally:
        await _teardown(beaver_db, runtime, [name], pumps=pumps)


@pytest.mark.asyncio
async def test_enable_after_disable_roundtrip_monotonic_seq(beaver_db: AsyncBeaverDB) -> None:
    """§C: disable → parked report → disabled; enable → ready report → enabled;
    both confirms are report-convergent and the mint seq stays monotonic."""
    runtime = _runtime(beaver_db)
    runtime.mount_agents({"pinger": _tool_agent(beaver_db)})
    pumps = _start_executor(runtime)
    name, spec = _catalog_spec("pinger")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("pinger"), pool=1)
        instance_id = "pinger-1"

        await runtime.disable_instance(name, instance_id)
        assert await _instance_state(runtime, name, instance_id) == ActivityState.DISABLED

        await runtime.enable_instance(name, instance_id)
        assert await _instance_state(runtime, name, instance_id) == ActivityState.ENABLED

        assert runtime._pending_controls == {}
        assert runtime._control_sequence[(name, instance_id)] >= 2
    finally:
        await _teardown(beaver_db, runtime, [name], pumps=pumps)


@pytest.mark.asyncio
async def test_orphan_report_is_visible_warning_never_applied(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture
) -> None:
    """§C/rule 9: a report that does not correspond to a pending mint (no
    ledger hit) is consumed as a visible WARNING and never applied."""
    from legio.runtime import DISABLE_INSTANCE, STATE_REPORT_TASK

    runtime = _runtime(beaver_db)
    pumps = _start_executor(runtime)
    name, spec = _catalog_spec("pinger")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("pinger"), pool=1)
        instance_id = "pinger-1"

        # The one-shot mint (no mounted agent honors, no report ever arrives)
        # registers a pending ledger entry for (instance, disable, seq=1).
        await runtime.manager.submit_task(DISABLE_INSTANCE, name, instance_id, action="disable")
        await asyncio.sleep(0.2)  # let the mint land and register the ledger entry
        assert runtime._pending_controls != {}

        # A forged report claiming seq 99 can never match a pending mint.
        await runtime._state_reports.put(
            AgentStateReport(
                instance_id=instance_id,
                action=ControlAction.DISABLE,
                seq=99,
                state=ReportedState.PARKED,
            ).model_dump(mode="json"),
            priority=0.0,
        )
        await runtime.manager.submit_task(STATE_REPORT_TASK)
        with caplog.at_level(logging.WARNING, logger="legio.runtime"):
            await asyncio.sleep(0.2)

        assert await _instance_state(runtime, name, instance_id) == ActivityState.ENABLED
        assert await runtime._state_reports.count() == 0  # consumed
        assert runtime._pending_controls != {}  # the real pending entry is untouched
        assert any("orphan" in message for message in caplog.messages)
    finally:
        await _teardown(beaver_db, runtime, [name], pumps=pumps)


@pytest.mark.asyncio
async def test_missing_report_surfaces_visible_error_no_optimistic_write(
    beaver_db: AsyncBeaverDB,
) -> None:
    """§C/rule 9: an enabled instance whose agent never reports (controls are
    dropped, so no report can arrive) raises a visible ``RecoverableError``
    within the budget — and the Registry is never written optimistically."""
    lifecycle = LifecycleConfig(default=LifecycleParams(drain_timeout=0.3, drain_interval=0.05))
    runtime = _runtime(beaver_db, lifecycle=lifecycle)
    runtime.mount_agents({"pinger": _tool_agent(beaver_db)})
    pumps = _start_executor(runtime)
    name, spec = _catalog_spec("pinger")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("pinger"), pool=1)
        instance_id = "pinger-1"

        # Force the missing-report path: the mounted agent drops every control
        # (no verifier) so the disable order is never honored and never reported.
        runtime._agents[name]._control_verifier = None

        with pytest.raises(RecoverableError, match="no state report") as excinfo:
            await runtime.disable_instance(name, instance_id)
        assert "no state report" in excinfo.value.args[0]
        assert await _instance_state(runtime, name, instance_id) == ActivityState.ENABLED
    finally:
        await _teardown(beaver_db, runtime, [name], pumps=pumps)


@pytest.mark.asyncio
async def test_destroy_confirm_stays_structural(beaver_db: AsyncBeaverDB) -> None:
    """§C/LEG-087: destroy's confirm is the bring-up record reaching ``success``
    (structural) — the terminating report is informational, never awaited."""
    runtime = _runtime(beaver_db)
    runtime.mount_agents({"pinger": _tool_agent(beaver_db)})
    pumps = _start_executor(runtime)
    name, spec = _catalog_spec("pinger")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("pinger"), pool=1)
        instance_id = "pinger-1"
        task_id = runtime._instance_tasks[(name, instance_id)]

        await runtime.destroy_instance(name, instance_id)

        record = await runtime.manager.status(task_id)
        assert record is not None and record.status == TaskStatus.SUCCESS
        assert await _instance_state(runtime, name, instance_id) is None
    finally:
        await _teardown(beaver_db, runtime, [name], pumps=pumps)


# --- §D: boot registers the report fact + intake footprint ---------------------


@pytest.mark.asyncio
async def test_boot_registers_the_report_intake(beaver_db: AsyncBeaverDB) -> None:
    """§C/§D: the Runtime registers the ``STATE_REPORT`` fact at construction and
    owns the ``state_report`` intake queue (node-internal, rule 13)."""
    from legio.runtime import STATE_REPORT_TASK

    runtime = _runtime(beaver_db)
    assert inspect.iscoroutinefunction(runtime.manager._registry[STATE_REPORT_TASK])
    assert runtime._state_reports is runtime._db.queue(STATE_REPORT_SCOPE)
    assert await runtime._state_reports.count() == 0
