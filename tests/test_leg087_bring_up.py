"""Contract tests for LEG-087 — real bring-up + lifecycle facts minting control.

The one-shot bring-up (record SUCCESS instantly) is the *default* Runtime vehicle
when no boot has mounted agents. The real bring-up the boot wires (LEG-087 §A) is
a **parked async generator** that spawns the instance's own ``standing_loop``
(LEG-082) and awaits it: while the agent lives the record reads ``running``, and
when the agent honors a ``terminate_with_drain`` and exits its own loop the
record reaches ``success`` at that same structural moment — never a timer or a
poll (rule 8).

The instance life verbs (enable / disable / destroy) never couple the instance
to Manager control-mode. They are **orders to the agent**: the Runtime mints a
signed ``ControlMessage`` (its per-boot key, per-instance monotonic seq) at
control priority on the class queue and confirms by bounded Manager reads
(§5.8, rule 8 exception). This module proves that the *real*, mounted path (the
same one ``boot_node`` wires, §C) honors those orders end-to-end: only a valid
signature/target/sequence terminates the loop, so a green test is itself the
proof the mint, deposit, signature and honor all held.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest
from beaver import AsyncBeaverDB

from legio.agents.tool_agent import ToolAgent
from legio.flow import ControlAction, ControlVerifier, sign_control
from legio.manager import TaskStatus
from legio.patterns import load_patterns
from legio.runtime import BRING_UP_TASK, Runtime
from legio.tools import AvailableToolsRegistry

NODE_ID = "runtime-a@host1"
KEY = bytes(range(32))


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
        implementation="tests.test_leg087_bring_up.echo_text",
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
    """The node's executor pool (LEG-085/§A): a real bring-up occupies one
    whole dispatch for the agent's life (a parked generator awaiting the
    standing loop), so the node runs one executor per life-task **and** spares
    for facts — the tests start a pool the same way a booted node must.
    beaver's ``get`` is atomic so the pumps share the pending queue safely."""

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


async def _record_snapshot(runtime: Runtime, task_id: str) -> TaskStatus | None:
    record = await runtime.manager.status(task_id)
    return None if record is None else record.status


# --- §A: the real bring-up as a parked async generator ------------------------


@pytest.mark.asyncio
async def test_mount_replaces_one_shot_with_parked_generator(beaver_db) -> None:
    """§A seam: an unmounted Runtime registers the one-shot bring-up (a coroutine);
    ``mount_agents`` (the boot's step, §C) registers the real bring-up — an async
    generator — in its place."""
    runtime = _runtime(beaver_db)
    assert inspect.iscoroutinefunction(runtime.manager._registry[BRING_UP_TASK])
    assert not inspect.isasyncgenfunction(runtime.manager._registry[BRING_UP_TASK])

    runtime.mount_agents({"pinger": _tool_agent(beaver_db)})

    assert inspect.isasyncgenfunction(runtime.manager._registry[BRING_UP_TASK])
    pumps = _start_executor(runtime)
    try:
        name, spec = _catalog_spec("pinger")
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("pinger"), pool=1)
        instance_id = "pinger-1"
        task_id = runtime._instance_tasks[(name, instance_id)]
        assert await _record_snapshot(runtime, task_id) == TaskStatus.RUNNING
    finally:
        await _teardown(beaver_db, runtime, ["pinger"], pumps=pumps)


@pytest.mark.asyncio
async def test_real_bring_up_fails_visibly_when_agent_not_mounted(beaver_db) -> None:
    """Rule 9, §A: a real bring-up for a class whose agent was never mounted
    refuses loudly — the bring-up record FAILS, naming the class, and the
    bounded confirm neither hangs nor pretends the vehicle is up."""
    runtime = _runtime(beaver_db)
    runtime.mount_agents({"other": _tool_agent(beaver_db)})
    pumps = _start_executor(runtime)
    name, spec = _catalog_spec("pinger")
    try:
        with pytest.raises(Exception, match="no standing agent mounted") as excinfo:
            await runtime.create_class(spec, spec_yaml=_atomic_yaml("pinger"), pool=1)
        assert "pinger" in excinfo.value.args[0]
    finally:
        await _teardown(beaver_db, runtime, [name], pumps=pumps)


@pytest.mark.asyncio
async def test_real_bring_up_stays_running_until_structural_success(beaver_db) -> None:
    """§A/§D: while the agent lives the record reads ``running`` (confirmed on
    create, still read while parked); a ``terminate_with_drain`` ends the agent's
    own loop and the record reaches ``success`` at that same structural moment."""
    runtime = _runtime(beaver_db)
    runtime.mount_agents({"pinger": _tool_agent(beaver_db)})
    pumps = _start_executor(runtime)
    name, spec = _catalog_spec("pinger")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("pinger"), pool=1)
        instance_id = "pinger-1"
        task_id = runtime._instance_tasks[(name, instance_id)]
        for _ in range(5):
            assert await _record_snapshot(runtime, task_id) == TaskStatus.RUNNING
            await asyncio.sleep(0.01)

        await runtime.destroy_instance(name, instance_id)

        assert await _record_snapshot(runtime, task_id) == TaskStatus.SUCCESS
        assert await runtime.get_instance(name, instance_id) is None
        assert (name, instance_id) not in runtime._instance_tasks
    finally:
        await _teardown(beaver_db, runtime, [name], pumps=pumps)


# --- §B: the verbs are orders to the agent, honored by the mounted loop -------


@pytest.mark.asyncio
async def test_disable_enable_leave_the_loop_alive_and_lock_the_sequence(
    beaver_db,
) -> None:
    """§B real path: disable parks the mounted standing loop (record stays
    ``running``), enable resumes it, terminate ends it — all honored only after
    a valid signature, so the green run proves each minted order arrived. The
    per-instance sequence is monotonic handling both orders."""
    runtime = _runtime(beaver_db)
    runtime.mount_agents({"pinger": _tool_agent(beaver_db)})
    pumps = _start_executor(runtime)
    name, spec = _catalog_spec("pinger")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("pinger"), pool=1)
        instance_id = "pinger-1"
        task_id = runtime._instance_tasks[(name, instance_id)]

        await runtime.disable_instance(name, instance_id)
        assert await _record_snapshot(runtime, task_id) == TaskStatus.RUNNING
        instance = await runtime.get_instance(name, instance_id)
        assert instance is not None

        await runtime.enable_instance(name, instance_id)
        assert await _record_snapshot(runtime, task_id) == TaskStatus.RUNNING
        assert runtime._control_sequence[(name, instance_id)] >= 2

        await runtime.destroy_instance(name, instance_id)

        assert await _record_snapshot(runtime, task_id) == TaskStatus.SUCCESS
        assert await runtime.get_instance(name, instance_id) is None
    finally:
        await _teardown(beaver_db, runtime, [name], pumps=pumps)


@pytest.mark.asyncio
async def test_second_live_loop_on_shared_agent_is_a_visible_warning(
    beaver_db, caplog
) -> None:
    """Debt guard (rule 9): a second standing loop spawning on the shared agent
    object of a class — the documented ``pool > 1`` control-state caveat — is a
    visible WARNING, never silent. The loop's death discards the tracked set."""
    import logging

    runtime = _runtime(beaver_db)
    runtime.mount_agents({"pinger": _tool_agent(beaver_db)})
    runtime._live_loops["pinger"] = {"ghost-instance"}
    pumps = _start_executor(runtime)
    name, spec = _catalog_spec("pinger")
    try:
        with caplog.at_level(logging.WARNING, logger="legio.runtime"):
            await runtime.create_class(spec, spec_yaml=_atomic_yaml("pinger"), pool=1)
        assert any("second_loop" in message for message in caplog.messages)
        assert any("ghost-instance" in message for message in caplog.messages)

        await runtime.destroy_instance(name, "pinger-1")

        assert "pinger-1" not in runtime._live_loops.get(name, set())
    finally:
        await _teardown(beaver_db, runtime, [name], pumps=pumps)


# --- §C: the boot wires the key, the verifiers and the real bring-up ----------


@pytest.mark.asyncio
async def test_boot_wires_key_verifiers_and_mount(beaver_db, node_dirs) -> None:
    """§C: ``boot_node`` hands the Runtime its control key and the materialized
    agents with verify-only handles injected; the engine's bring-up is the
    parked generator. The injected verifier accepts exactly the messages the
    Runtime's key signs (LEG-082 symmetry)."""
    from legio.config import load
    from legio.materializer import boot_node

    loaded = load(node_dirs["config"], env={})
    booted = await boot_node(
        loaded,
        db=beaver_db,
        control_key=KEY,
    )

    assert set(booted.runtime._agents) == {"pinger"}
    assert booted.runtime._control_key == KEY
    assert inspect.isasyncgenfunction(booted.runtime.manager._registry[BRING_UP_TASK])
    agent = booted.runtime._agents["pinger"]
    assert agent._control_verifier is not None
    demo = sign_control(
        KEY, target_instance="pinger-1", action=ControlAction.DISABLE, seq=7
    )
    assert agent._control_verifier.verify(demo)

    pumps = _start_executor(booted.runtime)
    name, spec = _catalog_spec("pinger")
    try:
        await booted.runtime.create_class(spec, spec_yaml=_atomic_yaml("pinger"), pool=1)
        instance_id = "pinger-1"
        task_id = booted.runtime._instance_tasks[(name, instance_id)]
        for _ in range(5):
            assert await _record_snapshot(booted.runtime, task_id) == TaskStatus.RUNNING
            await asyncio.sleep(0.01)

        await booted.runtime.destroy_instance(name, instance_id)

        assert await _record_snapshot(booted.runtime, task_id) == TaskStatus.SUCCESS
        assert await booted.runtime.get_instance(name, instance_id) is None
    finally:
        await _teardown(beaver_db, booted.runtime, [name], pumps=pumps)


@pytest.mark.asyncio
async def test_boot_without_key_derives_a_random_per_boot_key(beaver_db, node_dirs) -> None:
    """§C: a boot with no injected key derives one per boot — two boots yield two
    distinct keys, and both stay out of any log line (rule 11: never logged)."""
    from legio.config import load
    from legio.materializer import boot_node

    loaded = load(node_dirs["config"], env={})
    first = await boot_node(loaded, db=beaver_db)
    second = await boot_node(loaded, db=beaver_db)

    key_a, key_b = first.runtime._control_key, second.runtime._control_key
    assert isinstance(key_a, bytes) and len(key_a) >= 16
    assert isinstance(key_b, bytes) and len(key_b) >= 16
    assert key_a != key_b


# --- shared helpers ------------------------------------------------------------


def _catalog_spec(name: str):
    catalog = load_patterns(_atomic_yaml(name))
    spec = catalog.specs[name]
    return spec.name, spec


# --- boot fixture (§C): a minimal one-tool node -------------------------------


@pytest.fixture
def node_dirs(tmp_path) -> dict[str, str]:
    """A node filesystem: one tool pattern dir + tools.yaml + legio.yaml."""

    tool_dir = tmp_path / "patterns" / "tool"
    tool_dir.mkdir(parents=True)
    (tool_dir / "pinger.yaml").write_text(_atomic_yaml("pinger"), encoding="utf-8")
    for sub in ("linguistic", "composite"):
        (tmp_path / "patterns" / sub).mkdir(parents=True)
    tools_file = tmp_path / "tools.yaml"
    tools_file.write_text(
        "available_tools:\n"
        "  pinger:\n"
        '    implementation: "tests.test_leg087_bring_up.echo_text"\n'
        "    policy:\n"
        "      timeout: 30\n"
        "      retries: 0\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "node.db"
    config = tmp_path / "legio.yaml"
    config.write_text(
        f"""\
node:
  id: "boot@test"
database:
  db_path: "{db_path}"
patterns:
  tool: "{tool_dir}"
  linguistic: "{tmp_path / 'patterns' / 'linguistic'}"
  composite: "{tmp_path / 'patterns' / 'composite'}"
tools:
  config: "{tools_file}"
api:
  clients:
    client-a:
      agents: null
services:
  llm:
    base_url: "http://llm.test"
    model: "llm-model"
""",
        encoding="utf-8",
    )
    return {"config": str(config), "db": str(db_path)}