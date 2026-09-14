"""Contract tests for LEG-088 — node control intake (`node_ops`) + Runtime wiring.

The source of every ``origin: operator`` lifecycle order is an operator
(CLI/API/peer). That source reaches the Runtime through the Runtime's own
decoupled intake — the ``node_ops`` queue — drained **via a Manager fact**
(the ``NODE_OP`` polling fact), never pumped by the Runtime itself. The
operator surface never touches an agent queue and never mints a message: each
intent is validated by the Runtime's decision logic, then relayed to the
corresponding lifecycle verb (LEG-087), which mints and deposits the signed
``ControlMessage``.

Rule 8 holds: the intake drain never sleeps. Each ``NODE_OP`` dispatch drains
one intent and re-submits itself while work remains — the decision of *whether*
to schedule the next drain is the presence of work on the intake (a data read,
never ``asyncio.sleep``).

Tests that let a ``NODE_OP`` drain run drive ``manager.run()`` in a pump pool
(≥2 executors): a drain dispatch occupies its executor while it awaits the
lifecycle confirm, so the pool needs a spare to execute the lifecycle fact
itself — the LEG-087 executor-occupancy topology, exactly as a booted node
runs it (AGENT_LIFECYCLE §6.1).
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

import pytest
from beaver import AsyncBeaverDB

from legio.agents.tool_agent import ToolAgent
from legio.errors import RecoverableError
from legio.flow import ControlVerifier
from legio.manager import TaskStatus
from legio.naming import queue_key
from legio.patterns import load_patterns
from legio.registry import ActivityState
from legio.runtime import NODE_OP_TASK, NodeOp, Runtime
from legio.tools import AvailableToolsRegistry

NODE_ID = "runtime-a@host1"
KEY = bytes(range(32))

# Beaver dict scopes the Manager/Registry legitimately own. The Runtime's own
# share is exactly ``gates`` + ``node_ops`` (rule 13) — the only additions a
# fully-exercised Runtime may make to the tree's footprint.
_MANAGER_AND_REGISTRY_DICTS = {
    "tasks",
    "control",
    "catalog",
    "instances",
    "yaml_cache",
}


# --- domain-free pattern fixtures (rule 7) -----------------------------------


def echo_text(text: str) -> dict:
    """A fictitious domain-free tool (the pattern's own model)."""
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


def _runtime(db: AsyncBeaverDB) -> Runtime:
    return Runtime(db, node_id=NODE_ID, control_key=KEY)


def _tool_agent(db: AsyncBeaverDB) -> ToolAgent:
    registry = AvailableToolsRegistry()
    registry.declare(
        "pinger",
        implementation="tests.test_leg088_node_ops.echo_text",
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
    """The node's executor pool (AGENT_LIFECYCLE §6.1, LEG-087): a ``NODE_OP``
    drain occupies its executor while it awaits the lifecycle confirm, and a
    real bring-up occupies one for the agent's life — the pool runs one executor
    per occupant **plus spares for facts**, exactly as a booted node must."""

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


async def _wait_until(
    predicate: Callable[[], Any], *, timeout: float = 3.0
) -> bool:
    """Poll a test predicate (sync or async) until it holds or the budget is
    exhausted. Test-side clock wait only — the engine itself never sleeps
    (rule 8)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        result = predicate()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return True
        await asyncio.sleep(0.01)
    return False


async def _intake_empty(runtime: Runtime) -> bool:
    return await runtime._node_ops.count() == 0


async def _instance_state(
    runtime: Runtime, class_name: str, instance_id: str
) -> ActivityState | None:
    record = await runtime.registry.get_instance(class_name, instance_id)
    return None if record is None else record.state


async def _record_status(runtime: Runtime, task_id: str) -> TaskStatus | None:
    record = await runtime.manager.status(task_id)
    return None if record is None else record.status


async def _state_is(
    runtime: Runtime, class_name: str, instance_id: str, expected: ActivityState
) -> bool:
    return await _instance_state(runtime, class_name, instance_id) == expected


async def _state_is_none(runtime: Runtime, class_name: str, instance_id: str) -> bool:
    return await _instance_state(runtime, class_name, instance_id) is None


async def _status_is(runtime: Runtime, task_id: str, expected: TaskStatus) -> bool:
    return await _record_status(runtime, task_id) == expected


async def _scopes(db: AsyncBeaverDB) -> tuple[set[str], set[str]]:
    """Distinct beaver dict/queue scopes the whole tree has touched (footprint pin)."""
    cursor = await db.connection.execute("SELECT DISTINCT dict_name FROM __beaver_dicts__")
    dicts = {row[0] for row in await cursor.fetchall()}
    cursor = await db.connection.execute(
        "SELECT DISTINCT queue_name FROM __beaver_priority_queues__"
    )
    queues = {row[0] for row in await cursor.fetchall()}
    return dicts, queues


def _catalog_spec(name: str):
    catalog = load_patterns(_atomic_yaml(name))
    spec = catalog.specs[name]
    return spec.name, spec


# --- §A: the ``node_ops`` intake queue ---------------------------------------


@pytest.mark.asyncio
async def test_deposit_rejects_unknown_verb_and_never_mints(beaver_db) -> None:
    """§A/rule 9: the Runtime decides which intents a caller may deposit — an
    unknown verb is refused visibly at the intake, nothing enters ``node_ops``
    and nothing reaches any agent queue."""
    runtime = _runtime(beaver_db)
    name, spec = _catalog_spec("pinger")
    await runtime.create_class(spec, spec_yaml=_atomic_yaml("pinger"), pool=0)

    with pytest.raises(RecoverableError, match="unknown node op verb") as excinfo:
        await runtime.deposit_node_op("make_coffee", name)
    assert "make_coffee" in excinfo.value.args[0]

    assert await runtime._db.queue("node_ops").count() == 0
    assert await runtime._db.queue(queue_key(name)).count() == 0


@pytest.mark.asyncio
async def test_deposit_requires_existing_class_and_instance(beaver_db) -> None:
    """§A/rule 9: intents address only live catalog entries — an unknown class
    or an unknown instance is refused visibly at the intake."""
    runtime = _runtime(beaver_db)
    name, spec = _catalog_spec("pinger")
    await runtime.create_class(spec, spec_yaml=_atomic_yaml("pinger"), pool=0)

    with pytest.raises(RecoverableError, match="unknown class"):
        await runtime.deposit_node_op("enable_class", "ghost-class")
    with pytest.raises(RecoverableError, match="unknown instance"):
        await runtime.deposit_node_op("enable_instance", name, "pinger-99")

    assert await runtime._db.queue("node_ops").count() == 0


@pytest.mark.asyncio
async def test_instance_intent_requires_an_instance_and_class_verb_forbids_one(
    beaver_db,
) -> None:
    """§A: the typed intent shape is enforced at the intake — instance verbs
    require the instance, class verbs forbid it."""
    runtime = _runtime(beaver_db)
    name, spec = _catalog_spec("pinger")
    await runtime.create_class(spec, spec_yaml=_atomic_yaml("pinger"), pool=0)

    with pytest.raises(RecoverableError, match="requires an instance"):
        await runtime.deposit_node_op("disable_instance", name)
    with pytest.raises(RecoverableError, match="does not take an instance"):
        await runtime.deposit_node_op("disable_class", name, "pinger-1")

    assert await runtime._db.queue("node_ops").count() == 0


@pytest.mark.asyncio
async def test_deposit_queues_a_typed_intent_and_never_touches_the_agent_queue(
    beaver_db,
) -> None:
    """§A/§C: depositing an intent places only the typed payload on ``node_ops``
    and schedules the ``NODE_OP`` drain — the operator surface never touches the
    agent's queue and mints nothing at deposit time."""
    runtime = _runtime(beaver_db)
    name, spec = _catalog_spec("pinger")
    await runtime.create_class(spec, spec_yaml=_atomic_yaml("pinger"), pool=0)

    task_id = await runtime.deposit_node_op("enable_class", name)

    assert await runtime._node_ops.count() == 1
    item = await runtime._node_ops.peek()
    assert item is not None
    op = NodeOp.model_validate(item.data)
    assert op.verb == "enable_class"
    assert op.class_name == name
    assert op.instance_id is None
    assert await runtime._db.queue(queue_key(name)).count() == 0

    record = await runtime.manager.status(task_id)
    assert record is not None and record.name == NODE_OP_TASK
    assert record.status == TaskStatus.PENDING


# --- §B: Runtime → Manager wiring (the NODE_OP drain) -------------------------


@pytest.mark.asyncio
async def test_intent_reaches_instance_control_on_the_mounted_vehicle(
    beaver_db,
) -> None:
    """§B: an operator intent on ``node_ops`` is drained by the ``NODE_OP``
    fact, relayed to the lifecycle verb, and reaches the instance as a signed
    control message on its class queue (minted only by the Runtime); the
    Registry mirrors the result **only after** the standing loop honors the
    control and its honor report converges (LEG-095 — no optimistic posteriori
    write)."""
    runtime = _runtime(beaver_db)
    runtime.mount_agents({"pinger": _tool_agent(beaver_db)})
    pumps = _start_executor(runtime)
    name, spec = _catalog_spec("pinger")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("pinger"), pool=1)
        instance_id = "pinger-1"

        await runtime.deposit_node_op("disable_instance", name, instance_id)

        assert await _wait_until(lambda: _intake_empty(runtime))
        assert await _wait_until(
            lambda: _state_is(runtime, name, instance_id, ActivityState.DISABLED)
        )
        assert runtime._control_sequence[(name, instance_id)] == 1
        assert runtime._pending_controls == {}
        assert await runtime._state_reports.count() == 0
    finally:
        await _teardown(beaver_db, runtime, [name], pumps=pumps)


@pytest.mark.asyncio
async def test_mounted_disable_enable_destroy_intents_honored_between_dispatches(
    beaver_db,
) -> None:
    """§B real path: disable/enable/destroy intents relayed through ``node_ops``
    are honored by the mounted standing loop **between its dispatches** — the
    same signed-message proof as LEG-087, sourced from operator intents. Each
    intent is deposited only after the previous one is mirrored (test-side
    serialization; the engine itself stays concurrent)."""
    runtime = _runtime(beaver_db)
    runtime.mount_agents({"pinger": _tool_agent(beaver_db)})
    pumps = _start_executor(runtime)
    name, spec = _catalog_spec("pinger")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("pinger"), pool=1)
        instance_id = "pinger-1"
        task_id = runtime._instance_tasks[(name, instance_id)]

        await runtime.deposit_node_op("disable_instance", name, instance_id)
        assert await _wait_until(
            lambda: _state_is(runtime, name, instance_id, ActivityState.DISABLED)
        )

        await runtime.deposit_node_op("enable_instance", name, instance_id)
        assert await _wait_until(
            lambda: _state_is(runtime, name, instance_id, ActivityState.ENABLED)
        )
        record = await runtime.manager.status(task_id)
        assert record is not None and record.status == TaskStatus.RUNNING
        assert runtime._control_sequence[(name, instance_id)] >= 2

        await runtime.deposit_node_op("destroy_instance", name, instance_id)
        assert await _wait_until(
            lambda: _status_is(runtime, task_id, TaskStatus.SUCCESS)
        )
        assert await _wait_until(
            lambda: _state_is_none(runtime, name, instance_id)
        )
        assert (name, instance_id) not in runtime._instance_tasks
    finally:
        await _teardown(beaver_db, runtime, [name], pumps=pumps)


@pytest.mark.asyncio
async def test_invalid_intent_on_the_intake_surfaces_visibly_and_never_mints(
    beaver_db,
) -> None:
    """§C/rule 9: an unknown verb deposited straight onto ``node_ops`` (a
    tampered intake) fails the drain **visibly** — the ``NODE_OP`` task records
    ``failed`` naming the offending verb, and no control message is ever minted
    or deposited on any agent queue."""
    runtime = _runtime(beaver_db)
    name, spec = _catalog_spec("pinger")
    await runtime.create_class(spec, spec_yaml=_atomic_yaml("pinger"), pool=0)
    await runtime._db.queue("node_ops").put(
        {"verb": "make_coffee", "class_name": name}, priority=0.0
    )
    task_id = await runtime.manager.submit_task(NODE_OP_TASK)
    pumps = _start_executor(runtime)
    try:
        assert await _wait_until(lambda: _status_is(runtime, task_id, TaskStatus.FAILED))
        record = await runtime.manager.status(task_id)
        assert record is not None and "make_coffee" in (record.error or "")
        assert await runtime._db.queue(queue_key(name)).count() == 0
        assert await _intake_empty(runtime)
    finally:
        await _teardown(beaver_db, runtime, [name], pumps=pumps)


# --- §C: footprint and decision gate ------------------------------------------


@pytest.mark.asyncio
async def test_footprint_is_exactly_gates_plus_node_ops(beaver_db) -> None:
    """§C/rule 13: exercising the operator surface leaves the Runtime's beaver
    footprint exactly ``gates`` + ``node_ops`` — every other scope is Manager,
    Registry or flow-owned (naming never collides with the Manager's
    ``control`` scope). The LEG-095 intake scope ``state_report`` exists as
    another Runtime-owned handle, but it only materializes on its first deposit
    (no instance lives here, so no report is ever put — the pin holds)."""
    runtime = _runtime(beaver_db)
    name, spec = _catalog_spec("pinger")
    await runtime.create_class(spec, spec_yaml=_atomic_yaml("pinger"), pool=0)
    await runtime.deposit_node_op("enable_class", name)

    dicts, queues = await _scopes(beaver_db)

    runtime_dicts = dicts - _MANAGER_AND_REGISTRY_DICTS
    assert runtime_dicts == {"gates"}
    assert queues == {"pending_tasks", "node_ops"}