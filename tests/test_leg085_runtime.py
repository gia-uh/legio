"""Contract tests for LEG-085 — the Runtime public face (§0/§5/§6/§12.5).

These tests define the public contract of ``legio.runtime.Runtime``: the
orchestrator and public face of the runtime triangle. Every mutation follows
**action → (Manager fact) → confirm (Manager read) → record (Registry,
posteriori)**. The class entry gate (§12.5) blocks submits into a disabled
class; the lifecycle verbs (create / recreate / enable / disable / destroy at
both levels) are implemented per §5/§6.1; the business ``submit``/``status``
ride the Manager's ``seed`` task (ARCHITECTURE §7.1/§7.7). The node owns the
executor: these tests drive ``manager.run()`` in a background pump, exactly as
a booted node would — the Runtime never pumps (it only confirms bounded reads).

Written red first against the designed surface.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from beaver import AsyncBeaverDB

from legio.agents.tool_agent import ToolAgent
from legio.config import LifecycleConfig, LifecycleParams
from legio.errors import InvalidNameError, RecoverableError
from legio.flow import ExecutionResultMessage
from legio.manager import TaskStatus
from legio.naming import queue_key, result_queue_key, validate_task_id
from legio.patterns import load_patterns
from legio.patterns.schema1 import AgentSpec, AgentType, InputContract, IOType, OutputContract
from legio.registry import ActivityState
from legio.runtime import BRING_UP_TASK, SEED_TASK, Runtime
from legio.tools import AvailableToolsRegistry

NODE_ID = "runtime-a@host1"


# --- domain-free pattern fixtures (rule 7) -----------------------------------


def _atomic_yaml(name: str, *, main: bool = False) -> str:
    main_line = "main: true\n" if main else ""
    return f"""\
name: {name}
type: atomic
kind: tool
{main_line}input:
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


def _composite_yaml(name: str, branch: list[str]) -> str:
    branch_line = "  - - " + "\n    - ".join(branch) + "\n"
    return f"""\
name: {name}
type: composite
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
branches:
{branch_line}"""


def _load_atomic_spec(name: str) -> tuple[str, AgentSpec]:
    catalog = load_patterns(_atomic_yaml(name))
    spec = catalog.specs[name]
    return spec.name, spec


def _composite_spec(name: str, *deps: str) -> tuple[str, AgentSpec]:
    """Composite spec built via pydantic (loader's reuse-reference check is
    deferred to the boot catalog; the lifecycle tests own the dependency graph)."""
    contract_schema = {"type": "object", "properties": {"text": {"type": "string"}}}
    spec = AgentSpec(
        type=AgentType.COMPOSITE,
        kind=None,
        name=name,
        input=InputContract(input_as=name, input_type=IOType.JSON, input_schema=contract_schema),
        output=OutputContract(output_as=name, output_type=IOType.JSON, output_schema=contract_schema),
        branches=[list(deps)],
    )
    return name, spec


def _runtime(
    db: AsyncBeaverDB,
    *,
    node_id: str = NODE_ID,
    lifecycle: LifecycleConfig | None = None,
) -> Runtime:
    return Runtime(db, node_id=node_id, lifecycle=lifecycle)


def _start_executor(runtime: Runtime) -> asyncio.Task:
    """The node's executor loop (LEG-085): drive the Manager until cancelled.
    The Runtime never pumps — the node (and these tests) own the pump."""

    async def _loop() -> None:
        while True:
            await runtime.manager.run()
            await asyncio.sleep(0)

    return asyncio.create_task(_loop())


async def _teardown(
    db: AsyncBeaverDB, runtime: Runtime, names: list[str], *, pump: asyncio.Task | None = None
) -> None:
    for name in reversed(names):
        try:
            await runtime.destroy_class(name, mode="now")
        except RecoverableError as exc:  # teardown is best effort; stay visible
            print(f"teardown skipped class={name}: {exc}")
    if pump is not None:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)


async def _expect_seed_success(runtime: Runtime, task_id: str) -> None:
    """Wait, deterministically, for the node executor to dispatch the seed task."""
    for _ in range(200):
        record = await runtime.manager.status(task_id)
        if record is not None and record.status in (TaskStatus.SUCCESS, TaskStatus.FAILED):
            assert record.status == TaskStatus.SUCCESS
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"task {task_id} was never dispatched by the node executor")


# --- construction -------------------------------------------------------------


def test_runtime_requires_a_connected_beaver_db() -> None:
    with pytest.raises(TypeError):
        Runtime(cast(AsyncBeaverDB, None), node_id=NODE_ID)


def test_runtime_rejects_malformed_node_id(beaver_db) -> None:
    with pytest.raises(InvalidNameError):
        Runtime(beaver_db, node_id="local")


# --- create_class -------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_class_born_enabled_with_pool_one(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    name, spec = _load_atomic_spec("one-class")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("one-class"), pool=1)

        assert await runtime.class_state(name) == ActivityState.ENABLED
        gate = await beaver_db.dict("gates").fetch(name)
        assert gate is not None and gate["state"] == "enabled"

        instances = await runtime.list_instances(name)
        assert len(instances) == 1
        assert instances[0].instance_id == "one-class-1"
        assert instances[0].state == ActivityState.ENABLED

        task_id = runtime._instance_tasks[(name, "one-class-1")]
        task = await beaver_db.dict("tasks").fetch(task_id)
        assert task is not None and task["status"] == TaskStatus.SUCCESS.value
        assert task["name"] == BRING_UP_TASK
        assert task["result"] == "one-class-1"
    finally:
        await _teardown(beaver_db, runtime, [name], pump=pump)


@pytest.mark.asyncio
async def test_create_class_pool_zero_born_disabled_without_instances(
    beaver_db,
) -> None:
    runtime = _runtime(beaver_db)
    name, spec = _load_atomic_spec("lazy-class")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("lazy-class"), pool=0)

        assert await runtime.class_state(name) == ActivityState.DISABLED
        assert await runtime.list_instances(name) == []
        gate = await beaver_db.dict("gates").fetch(name)
        assert gate is not None and gate["state"] == "disabled"
    finally:
        await _teardown(beaver_db, runtime, [name])


@pytest.mark.asyncio
async def test_create_class_missing_dependency_born_disabled_with_disabled_instance(
    beaver_db,
) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    name, spec = _composite_spec("one-class", "two-class")
    try:
        await runtime.create_class(
            spec,
            spec_yaml=_composite_yaml("one-class", ["two-class"]),
            pool=1,
        )

        assert await runtime.class_state(name) == ActivityState.DISABLED
        instances = await runtime.list_instances(name)
        assert len(instances) == 1
        assert instances[0].state == ActivityState.DISABLED
        gate = await beaver_db.dict("gates").fetch(name)
        assert gate is not None and gate["state"] == "disabled"
    finally:
        await _teardown(beaver_db, runtime, [name], pump=pump)


@pytest.mark.asyncio
async def test_create_class_dependencies_satisfied_born_enabled(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    two_name, two_spec = _load_atomic_spec("two-class")
    one_name, one_spec = _composite_spec("one-class", "two-class")
    try:
        await runtime.create_class(two_spec, spec_yaml=_atomic_yaml("two-class"), pool=1)
        await runtime.create_class(
            one_spec,
            spec_yaml=_composite_yaml("one-class", ["two-class"]),
            pool=1,
        )

        assert await runtime.class_state(two_name) == ActivityState.ENABLED
        assert await runtime.class_state(one_name) == ActivityState.ENABLED
    finally:
        await _teardown(beaver_db, runtime, [one_name, two_name], pump=pump)


@pytest.mark.asyncio
async def test_create_class_of_existing_class_is_noop(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    name, spec = _load_atomic_spec("twice-class")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("twice-class"), pool=1)
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("twice-class"), pool=2)

        classes = await runtime.list_classes()
        assert [c.name for c in classes] == [name]
        assert len(await runtime.list_instances(name)) == 1
    finally:
        await _teardown(beaver_db, runtime, [name], pump=pump)


# --- create_instance ----------------------------------------------------------


@pytest.mark.asyncio
async def test_create_instance_of_missing_class_errors(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    with pytest.raises(KeyError):
        await runtime.create_instance("ghost-class", count=1)


@pytest.mark.asyncio
async def test_create_instance_mints_sequential_ids_and_follows_class_state(
    beaver_db,
) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    name, spec = _load_atomic_spec("many-class")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("many-class"), pool=0)
        await runtime.create_instance(name, count=2)

        instances = await runtime.list_instances(name)
        assert [i.instance_id for i in instances] == ["many-class-1", "many-class-2"]
        assert all(i.state == ActivityState.DISABLED for i in instances)
    finally:
        await _teardown(beaver_db, runtime, [name], pump=pump)


@pytest.mark.asyncio
async def test_create_instance_ids_stay_monotonic_across_destroy(beaver_db) -> None:
    """§5.1 ``seq`` monotonic intraboot: a destroy frees no id — a later create
    never reuses an id a still-live instance holds."""
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    name, spec = _load_atomic_spec("many-class")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("many-class"), pool=0)
        await runtime.create_instance(name, count=2)
        await runtime.destroy_instance(name, "many-class-1")

        assert [i.instance_id for i in await runtime.list_instances(name)] == ["many-class-2"]

        await runtime.create_instance(name, count=1)
        grown = await runtime.list_instances(name)
        assert [i.instance_id for i in grown] == ["many-class-2", "many-class-3"]
        assert len(grown) == 2
    finally:
        await _teardown(beaver_db, runtime, [name], pump=pump)


# --- enable / disable instance ------------------------------------------------


@pytest.mark.asyncio
async def test_disable_instance_pauses_and_records_disabled(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    name, spec = _load_atomic_spec("single-class")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("single-class"), pool=1)
        instance_id = "single-class-1"
        task_id = await runtime.disable_instance(name, instance_id)

        assert await runtime.manager.control_mode(task_id) == "pause"
        instance = await runtime.get_instance(name, instance_id)
        assert instance is not None and instance.state == ActivityState.DISABLED
        assert await runtime.class_state(name) == ActivityState.ENABLED
    finally:
        await _teardown(beaver_db, runtime, [name], pump=pump)


@pytest.mark.asyncio
async def test_enable_instance_resumes_and_records_enabled(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    name, spec = _load_atomic_spec("single-class")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("single-class"), pool=1)
        instance_id = "single-class-1"
        await runtime.disable_instance(name, instance_id)
        task_id = await runtime.enable_instance(name, instance_id)

        assert await runtime.manager.control_mode(task_id) == "run"
        instance = await runtime.get_instance(name, instance_id)
        assert instance is not None and instance.state == ActivityState.ENABLED
    finally:
        await _teardown(beaver_db, runtime, [name], pump=pump)


# --- enable / disable class ---------------------------------------------------


@pytest.mark.asyncio
async def test_disable_class_policy_a_instances_keep_draining(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    name, spec = _load_atomic_spec("drain-class")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("drain-class"), pool=1)
        await runtime.disable_class(name)

        gate = await beaver_db.dict("gates").fetch(name)
        assert gate is not None and gate["state"] == "disabled"
        assert await runtime.class_state(name) == ActivityState.DISABLED
        instance = await runtime.get_instance(name, "drain-class-1")
        assert instance is not None and instance.state == ActivityState.ENABLED
    finally:
        await _teardown(beaver_db, runtime, [name], pump=pump)


@pytest.mark.asyncio
async def test_disable_class_cascades_dependents_transitively(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    two_name, two_spec = _load_atomic_spec("two-class")
    one_name, one_spec = _composite_spec("one-class", "two-class")
    top_name, top_spec = _composite_spec("top-class", "one-class")
    try:
        await runtime.create_class(two_spec, spec_yaml=_atomic_yaml("two-class"), pool=1)
        await runtime.create_class(
            one_spec,
            spec_yaml=_composite_yaml("one-class", ["two-class"]),
            pool=1,
        )
        await runtime.create_class(
            top_spec,
            spec_yaml=_composite_yaml("top-class", ["one-class"]),
            pool=1,
        )
        assert await runtime.class_state(top_name) == ActivityState.ENABLED

        await runtime.disable_class(two_name)

        for cls in (two_name, one_name, top_name):
            assert await runtime.class_state(cls) == ActivityState.DISABLED
            gate = await beaver_db.dict("gates").fetch(cls)
            assert gate is not None and gate["state"] == "disabled"
        assert await runtime.class_dependents(two_name, transitive=True) == [
            one_name,
            top_name,
        ]
    finally:
        await _teardown(beaver_db, runtime, [top_name, one_name, two_name], pump=pump)


@pytest.mark.asyncio
async def test_cascade_is_not_auto_undone_by_enabling_the_dependency(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    two_name, two_spec = _load_atomic_spec("two-class")
    one_name, one_spec = _composite_spec("one-class", "two-class")
    try:
        await runtime.create_class(two_spec, spec_yaml=_atomic_yaml("two-class"), pool=1)
        await runtime.create_class(
            one_spec,
            spec_yaml=_composite_yaml("one-class", ["two-class"]),
            pool=1,
        )
        await runtime.disable_class(two_name)

        await runtime.enable_class(two_name)

        assert await runtime.class_state(two_name) == ActivityState.ENABLED
        assert await runtime.class_state(one_name) == ActivityState.DISABLED
    finally:
        await _teardown(beaver_db, runtime, [one_name, two_name], pump=pump)


@pytest.mark.asyncio
async def test_enable_class_brings_up_one_instance_when_none_exists(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    name, spec = _load_atomic_spec("stalled-class")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("stalled-class"), pool=0)
        assert await runtime.list_instances(name) == []

        await runtime.enable_class(name)

        instances = await runtime.list_instances(name)
        assert len(instances) == 1
        assert instances[0].state == ActivityState.ENABLED
        assert await runtime.class_state(name) == ActivityState.ENABLED
        gate = await beaver_db.dict("gates").fetch(name)
        assert gate is not None and gate["state"] == "enabled"
    finally:
        await _teardown(beaver_db, runtime, [name], pump=pump)


# --- destroy_instance ---------------------------------------------------------


@pytest.mark.asyncio
async def test_destroy_instance_cancels_and_confirms_terminal(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    name, spec = _load_atomic_spec("one-class")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("one-class"), pool=1)
        instance_id = "one-class-1"
        task_id = runtime._instance_tasks[(name, instance_id)]

        await runtime.destroy_instance(name, instance_id)

        # the one-shot bring-up fact is terminal (SUCCESS); a destroy that lands
        # on a not-yet-dispatched bring-up lands failed(cancelled) — either is
        # the visible terminal state of §5.7, never a live record.
        record = await beaver_db.dict("tasks").fetch(task_id)
        assert record is not None
        assert record["status"] in (TaskStatus.SUCCESS.value, TaskStatus.FAILED.value)
        assert await runtime.get_instance(name, instance_id) is None
        assert (name, instance_id) not in runtime._instance_tasks
    finally:
        await _teardown(beaver_db, runtime, [name], pump=pump)


@pytest.mark.asyncio
async def test_destroy_last_instance_leaves_class_disabled_but_existing(
    beaver_db,
) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    name, spec = _load_atomic_spec("one-class")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("one-class"), pool=1)

        await runtime.destroy_instance(name, "one-class-1")

        assert await runtime.class_state(name) == ActivityState.DISABLED
        assert await runtime.list_instances(name) == []
        assert name in [c.name for c in await runtime.list_classes()]
    finally:
        await _teardown(beaver_db, runtime, [name], pump=pump)


# --- destroy_instance terminal confirmation (audit finding #7 fix) ------------


@pytest.mark.asyncio
async def test_destroy_instance_confirms_only_an_actual_terminal_cancel(
    beaver_db,
) -> None:
    """§5.7 terminal confirmation, tightened: ``failed(cancelled)`` is the only
    FAILED fact destroy accepts as confirmed.

    The one-shot bring-up lands SUCCESS (a replayed cancel is then a terminal
    no-op, so SUCCESS is also confirmed); a still-pending cancel lands
    ``failed(cancelled)``. A bring-up that FAILED for any *other* reason is NOT
    a confirmed cancel — destroy refuses visibly (rule 9) instead of pretending
    the cancel processed.
    """
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    a, spec_a = _load_atomic_spec("one-class")
    b, spec_b = _load_atomic_spec("two-class")
    try:
        await runtime.create_class(spec_a, spec_yaml=_atomic_yaml(a), pool=1)
        await runtime.create_class(spec_b, spec_yaml=_atomic_yaml(b), pool=1)

        # a live-cancelled bring-up (failed(cancelled)) is the confirmed fact
        task_a = runtime._instance_tasks[(a, "one-class-1")]
        record_a = await beaver_db.dict("tasks").fetch(task_a)
        record_a["status"] = TaskStatus.FAILED.value
        record_a["error"] = "cancelled"
        await beaver_db.dict("tasks").set(task_a, record_a)
        await runtime.destroy_instance(a, "one-class-1")
        assert await runtime.get_instance(a, "one-class-1") is None

        # a bring-up that died for another reason is not a silent terminal
        task_b = runtime._instance_tasks[(b, "two-class-1")]
        record_b = await beaver_db.dict("tasks").fetch(task_b)
        record_b["status"] = TaskStatus.FAILED.value
        record_b["error"] = "vehicle crashed"
        await beaver_db.dict("tasks").set(task_b, record_b)
        with pytest.raises(RecoverableError):
            await runtime.destroy_instance(b, "two-class-1")
        assert await runtime.get_instance(b, "two-class-1") is not None
        assert (b, "two-class-1") in runtime._instance_tasks
    finally:
        await _teardown(beaver_db, runtime, [a, b], pump=pump)


# --- destroy_class ------------------------------------------------------------


@pytest.mark.asyncio
async def test_destroy_class_now_clears_queue_gate_and_class(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    name, spec = _load_atomic_spec("now-class")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("now-class"), pool=1)
        queue = beaver_db.queue(queue_key(name))
        task_id = await runtime.submit("client-a", ((name, name),), {"text": "x"})
        await _expect_seed_success(runtime, task_id)
        assert await queue.count() == 1

        await runtime.destroy_class(name, mode="now")

        assert await queue.count() == 0
        assert await beaver_db.dict("gates").fetch(name) is None
        assert await runtime.class_state(name) is None
        assert await runtime.get_cached_spec(name) == _atomic_yaml("now-class")
        assert await runtime.list_instances(name) == []
        seed = await beaver_db.dict("tasks").fetch(task_id)
        assert seed is not None and seed["name"] == SEED_TASK
    finally:
        await _teardown(beaver_db, runtime, [name], pump=pump)


@pytest.mark.asyncio
async def test_destroy_class_drain_waits_for_queue_to_empty(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    name, spec = _load_atomic_spec("drain-class")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("drain-class"), pool=1)
        task_id = await runtime.submit("client-a", ((name, name),), {"text": "x"})
        await _expect_seed_success(runtime, task_id)
        queue = beaver_db.queue(queue_key(name))
        assert await queue.count() == 1

        task = asyncio.create_task(runtime.destroy_class(name, mode="drain"))
        await asyncio.sleep(0.05)
        assert not task.done()
        assert await runtime.class_state(name) == ActivityState.ENABLED

        item = await queue.get(block=False)
        assert item is not None
        await task

        assert await runtime.class_state(name) is None
        assert await beaver_db.dict("gates").fetch(name) is None
    finally:
        await _teardown(beaver_db, runtime, [name], pump=pump)


@pytest.mark.asyncio
async def test_destroy_class_drain_times_out_visibly_and_leaves_class(
    beaver_db,
) -> None:
    runtime = _runtime(
        beaver_db,
        lifecycle=LifecycleConfig(
            default=LifecycleParams(drain_timeout=0.2, drain_interval=0.05)
        ),
    )
    pump = _start_executor(runtime)
    name, spec = _load_atomic_spec("stuck-class")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("stuck-class"), pool=1)
        task_id = await runtime.submit("client-a", ((name, name),), {"text": "x"})
        await _expect_seed_success(runtime, task_id)

        with pytest.raises(RecoverableError):
            await runtime.destroy_class(name, mode="drain")

        assert await runtime.class_state(name) == ActivityState.ENABLED
        assert await beaver_db.queue(queue_key(name)).count() == 1
        assert await beaver_db.dict("gates").fetch(name) == {
            "state": ActivityState.ENABLED.value
        }
    finally:
        await _teardown(beaver_db, runtime, [name], pump=pump)


@pytest.mark.asyncio
async def test_destroy_class_cascades_dependents(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    two_name, two_spec = _load_atomic_spec("two-class")
    one_name, one_spec = _composite_spec("one-class", "two-class")
    try:
        await runtime.create_class(two_spec, spec_yaml=_atomic_yaml("two-class"), pool=1)
        await runtime.create_class(
            one_spec,
            spec_yaml=_composite_yaml("one-class", ["two-class"]),
            pool=1,
        )

        await runtime.destroy_class(two_name, mode="now")

        assert await runtime.class_state(two_name) is None
        assert await runtime.class_state(one_name) == ActivityState.DISABLED
        assert await beaver_db.dict("gates").fetch(one_name) == {"state": "disabled"}
        assert await runtime.get_instance(one_name, "one-class-1") is not None
    finally:
        await _teardown(beaver_db, runtime, [one_name, two_name], pump=pump)


# --- recreate_class -----------------------------------------------------------


@pytest.mark.asyncio
async def test_recreate_class_from_cached_spec(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    name, spec = _load_atomic_spec("reuse-class")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("reuse-class"), pool=1)
        await runtime.destroy_class(name, mode="now")
        assert await runtime.class_state(name) is None

        await runtime.recreate_class(name, pool=1)

        assert await runtime.class_state(name) == ActivityState.ENABLED
        instances = await runtime.list_instances(name)
        assert len(instances) == 1 and instances[0].state == ActivityState.ENABLED
    finally:
        await _teardown(beaver_db, runtime, [name], pump=pump)


@pytest.mark.asyncio
async def test_recreate_class_of_existing_class_errors(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    name, spec = _load_atomic_spec("alive-class")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("alive-class"), pool=1)
        with pytest.raises(RecoverableError):
            await runtime.recreate_class(name, pool=1)
    finally:
        await _teardown(beaver_db, runtime, [name], pump=pump)


@pytest.mark.asyncio
async def test_recreate_class_of_uncached_class_errors(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    with pytest.raises(RecoverableError):
        await runtime.recreate_class("ghost-class", pool=1)


# --- business submit / status (mounted on the Manager seed, §7.1/§7.7) -------


@pytest.mark.asyncio
async def test_submit_mints_node_task_id_and_deposits_first_step(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    cls = "transform"
    _, spec = _load_atomic_spec(cls)
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml(cls), pool=1)

        task_id = await runtime.submit("client-a", ((cls, cls),), {"text": "hello"})
        validate_task_id(task_id)
        assert task_id.startswith(f"{NODE_ID}:")
        await _expect_seed_success(runtime, task_id)
        seed = await beaver_db.dict("tasks").fetch(task_id)
        assert seed is not None and seed["name"] == SEED_TASK
        assert seed["kwargs"]["client_id"] == "client-a"
        assert seed["kwargs"]["payload"] == {cls: {"text": "hello"}}

        item = await beaver_db.queue(queue_key(cls)).get(block=False)
        assert item.data["task_id"] == task_id
        assert item.data["current_index"] == 0
        assert item.data["level"] == 1
        assert item.data["end_of_level_queue"] == result_queue_key(task_id)
        assert item.data["payload"] == {cls: {"text": "hello"}}
    finally:
        await _teardown(beaver_db, runtime, [cls], pump=pump)


@pytest.mark.asyncio
async def test_submit_into_disabled_class_is_blocked_at_the_gate(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    cls = "gate-class"
    _, spec = _load_atomic_spec(cls)
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml(cls), pool=1)
        await runtime.disable_class(cls)
        before: list[str] = []
        async for key in beaver_db.dict("tasks").keys():
            before.append(key)

        with pytest.raises(RecoverableError):
            await runtime.submit("client-a", ((cls, cls),), {"text": "x"})

        after: list[str] = []
        async for key in beaver_db.dict("tasks").keys():
            after.append(key)
        assert after == before
        assert await beaver_db.queue(queue_key(cls)).count() == 0
    finally:
        await _teardown(beaver_db, runtime, [cls], pump=pump)


# --- scope decoupling (the Manager holds the task reality; §6) ----------------


@pytest.mark.asyncio
async def test_business_submit_lands_as_a_seed_task_in_manager_tasks(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    cls = "submit-class"
    _, spec = _load_atomic_spec(cls)
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml(cls), pool=1)
        task_id = await runtime.submit("client-a", ((cls, cls),), {"text": "x"})
        await _expect_seed_success(runtime, task_id)

        business = await beaver_db.dict("tasks").fetch(task_id)
        assert business is not None
        assert business["name"] == SEED_TASK
        assert business["kwargs"]["client_id"] == "client-a"
        assert business["kwargs"]["token"]["task_id"] == task_id

        await runtime.status(task_id, "client-a")  # reads back owner-scoped
    finally:
        await _teardown(beaver_db, runtime, [cls], pump=pump)


@pytest.mark.asyncio
async def test_runtime_owns_no_business_tasks_scope(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    cls = "scope-probe"
    _, spec = _load_atomic_spec(cls)
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml(cls), pool=1)
        await runtime.submit("client-a", ((cls, cls),), {"text": "x"})
        keys: list[str] = []
        async for key in beaver_db.dict("business_tasks").keys():
            keys.append(key)
        assert keys == []
    finally:
        await _teardown(beaver_db, runtime, [cls], pump=pump)


@pytest.mark.asyncio
async def test_runtime_status_flow_completed_and_owner_scoped(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    cls = "transform"
    _, spec = _load_atomic_spec(cls)
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml(cls), pool=1)

        task_id = await runtime.submit("client-a", ((cls, cls),), {"text": "hello"})
        await _expect_seed_success(runtime, task_id)

        registry = AvailableToolsRegistry()
        registry.declare(
            "transform",
            implementation="tests.test_leg085_runtime.fake_transform",
            policy={"timeout": 30, "retries": 0},
        )
        agent = ToolAgent(
            agent_id=cls,
            db=beaver_db,
            available_tools=registry,
            tool_name="transform",
            parameters={"text": "{transform.text}"},
            input_as=cls,
            output_as=cls,
        )
        steps = await agent.run()
        assert steps == 1

        entry = await runtime.status(task_id, "client-a")
        assert entry.state.value == "completed"
        assert entry.output == {cls: {"transformed": "HELLO"}}
        assert entry.result_key == result_queue_key(task_id)

        result_item = await beaver_db.queue(queue_key(result_queue_key(task_id))).peek()
        assert result_item is not None
        result = ExecutionResultMessage.model_validate(result_item.data)
        assert result.payload == {cls: {"transformed": "HELLO"}}

        with pytest.raises(PermissionError):
            await runtime.status(task_id, "client-b")
        with pytest.raises(KeyError):
            await runtime.status(f"{NODE_ID}:00000000-0000-0000-0000-000000000000", "client-a")
    finally:
        await _teardown(beaver_db, runtime, [cls], pump=pump)


# --- entry gate reads ---------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_row_is_written_only_by_runtime_lifecycle(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    cls = "gate-probe"
    _, spec = _load_atomic_spec(cls)
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml(cls), pool=0)
        assert await beaver_db.dict("gates").fetch(cls) == {"state": "disabled"}

        await runtime.enable_class(cls)
        assert await beaver_db.dict("gates").fetch(cls) == {"state": "enabled"}

        await runtime.disable_class(cls)
        assert await beaver_db.dict("gates").fetch(cls) == {"state": "disabled"}
    finally:
        await _teardown(beaver_db, runtime, [cls], pump=pump)


# --- executor ownership (the node pumps; the Runtime confirms reads only) ----


@pytest.mark.asyncio
async def test_node_drives_the_executor_through_the_manager(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    calls: list[int] = []

    async def build(n: int) -> int:
        calls.append(n)
        return n

    assert not hasattr(runtime, "register")
    assert not hasattr(runtime, "run")
    runtime.manager.register("build", build)
    await runtime.manager.submit_task("build", 7)
    assert await runtime.manager.run() == 1
    assert calls == [7]


def fake_transform(text: str) -> dict[str, Any]:
    return {"transformed": str(text).upper()}