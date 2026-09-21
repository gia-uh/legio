"""Contract tests for LEG-086 — the Runtime boot catalog (§4.3/§8, pools wiring).

These tests define the contract of ``Runtime.create_from_catalog`` (and the
pool-resolution surface): given the loaded ``Catalog`` and the node's
``PoolsConfig``, the Runtime brings the initial catalog state up in
**topological order** (leaves first) so dependents are created enabled (§8 step
4 / §4.2), resolving each class's pool by ``per_pattern > per_kind > default``
with an optional explicit invocation override winning over all of them
(§4.3/LEG-080). Pool size is intent; the catalog records the real count of
instances actually brought up. A dependency cycle in the catalog is rejected
visibly before anything is recorded (rule 9).
"""

from __future__ import annotations

import asyncio

import pytest
from beaver import AsyncBeaverDB

from legio.config import PoolsConfig
from legio.errors import RecoverableError
from legio.patterns import AgentKind, Catalog, load_patterns
from legio.patterns.schema1 import AgentSpec, AgentType, InputContract, IOType, OutputContract
from legio.registry import ActivityState
from legio.runtime import Runtime

NODE_ID = "boot-node@host1"


# --- domain-free fixtures (rule 7) -------------------------------------------


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


def _composite_spec(name: str, *deps: str) -> AgentSpec:
    contract_schema = {"type": "object", "properties": {"text": {"type": "string"}}}
    return AgentSpec(
        type=AgentType.COMPOSITE,
        kind=None,
        name=name,
        input=InputContract(input_as=name, input_type=IOType.JSON, input_schema=contract_schema),
        output=OutputContract(
            output_as=name, output_type=IOType.JSON, output_schema=contract_schema
        ),
        branches=[list(deps)],
    )


def _atomic_spec(name: str) -> AgentSpec:
    catalog = load_patterns(_atomic_yaml(name))
    return catalog.specs[name]


def _runtime(db: AsyncBeaverDB) -> Runtime:
    return Runtime(db, node_id=NODE_ID)


def _start_executor(runtime: Runtime) -> asyncio.Task:
    """The node's executor loop (LEG-085): the Runtime never pumps; the node
    owns the pump that drives the Manager's polling passes."""

    async def _loop() -> None:
        while True:
            await runtime.manager.run()
            await asyncio.sleep(0)

    return asyncio.create_task(_loop())


def _spec_yamls() -> dict[str, str]:
    return {
        "two-class": _atomic_yaml("two-class"),
        "one-class": _composite_yaml("one-class", ["two-class"]),
        "top-class": _composite_yaml("top-class", ["one-class"]),
    }


async def _teardown(
    db: AsyncBeaverDB, runtime: Runtime, names: list[str], *, pump: asyncio.Task | None = None
) -> None:
    for name in reversed(names):
        try:
            await runtime.destroy_class(name, mode="now")
        except RecoverableError as exc:
            print(f"teardown skipped class={name}: {exc}")
    if pump is not None:
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)


# --- topological order and born-enabled dependents (§8) ----------------------


@pytest.mark.asyncio
async def test_boot_orders_leaves_first_so_dependents_born_enabled(
    beaver_db,
) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    catalog = Catalog(
        specs={
            "two-class": _atomic_spec("two-class"),
            "one-class": _composite_spec("one-class", "two-class"),
            "top-class": _composite_spec("top-class", "one-class"),
        }
    )
    names = ["top-class", "one-class", "two-class"]
    try:
        await runtime.create_from_catalog(catalog, pools=PoolsConfig(), spec_yamls=_spec_yamls())

        for cls in names:
            assert await runtime.class_state(cls) == ActivityState.ENABLED, cls
        assert await runtime.class_state("two-class") == ActivityState.ENABLED
        assert await runtime.list_instances("one-class") != []
    finally:
        await _teardown(beaver_db, runtime, names, pump=pump)


@pytest.mark.asyncio
async def test_boot_pool_resolves_per_default_to_one(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    catalog = Catalog(specs={"two-class": _atomic_spec("two-class")})
    try:
        await runtime.create_from_catalog(catalog, pools=PoolsConfig())
        assert len(await runtime.list_instances("two-class")) == 1
    finally:
        await _teardown(beaver_db, runtime, ["two-class"], pump=pump)


@pytest.mark.asyncio
async def test_boot_pool_resolves_per_default_config(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    catalog = Catalog(specs={"two-class": _atomic_spec("two-class")})
    try:
        await runtime.create_from_catalog(
            catalog, pools=PoolsConfig(default=3), spec_yamls=_spec_yamls()
        )
        assert len(await runtime.list_instances("two-class")) == 3
    finally:
        await _teardown(beaver_db, runtime, ["two-class"], pump=pump)


@pytest.mark.asyncio
async def test_boot_pool_resolves_per_kind_tool(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    catalog = Catalog(specs={"two-class": _atomic_spec("two-class")})
    try:
        await runtime.create_from_catalog(
            catalog,
            pools=PoolsConfig(default=1, per_kind={AgentKind.TOOL: 2}),
            spec_yamls=_spec_yamls(),
        )
        assert len(await runtime.list_instances("two-class")) == 2
    finally:
        await _teardown(beaver_db, runtime, ["two-class"], pump=pump)


@pytest.mark.asyncio
async def test_boot_pool_resolves_per_pattern_highest(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    catalog = Catalog(specs={"two-class": _atomic_spec("two-class")})
    try:
        await runtime.create_from_catalog(
            catalog,
            pools=PoolsConfig(
                default=1, per_kind={AgentKind.TOOL: 2}, per_pattern={"two-class": 4}
            ),
            spec_yamls=_spec_yamls(),
        )
        assert len(await runtime.list_instances("two-class")) == 4
    finally:
        await _teardown(beaver_db, runtime, ["two-class"], pump=pump)


@pytest.mark.asyncio
async def test_boot_explicit_pool_override_wins_over_config(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    catalog = Catalog(specs={"two-class": _atomic_spec("two-class")})
    try:
        await runtime.create_from_catalog(
            catalog,
            pools=PoolsConfig(per_pattern={"two-class": 4}),
            pool_override=2,
            spec_yamls=_spec_yamls(),
        )
        assert len(await runtime.list_instances("two-class")) == 2
    finally:
        await _teardown(beaver_db, runtime, ["two-class"], pump=pump)


@pytest.mark.asyncio
async def test_boot_pool_zero_born_disabled(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    catalog = Catalog(specs={"two-class": _atomic_spec("two-class")})
    try:
        await runtime.create_from_catalog(catalog, pools=PoolsConfig(per_pattern={"two-class": 0}))
        assert await runtime.class_state("two-class") == ActivityState.DISABLED
        assert await runtime.list_instances("two-class") == []
    finally:
        await _teardown(beaver_db, runtime, ["two-class"], pump=pump)


@pytest.mark.asyncio
async def test_boot_caches_each_spec_yaml(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    catalog = Catalog(specs={"two-class": _atomic_spec("two-class")})
    try:
        await runtime.create_from_catalog(catalog, pools=PoolsConfig(), spec_yamls=_spec_yamls())
        assert await runtime.get_cached_spec("two-class") == _atomic_yaml("two-class")
    finally:
        await _teardown(beaver_db, runtime, ["two-class"], pump=pump)


# --- dependency cycle handling (§8 step 6) -----------------------------------


@pytest.mark.asyncio
async def test_boot_rejects_dependency_cycle_before_anything_recorded(
    beaver_db,
) -> None:
    runtime = _runtime(beaver_db)
    catalog = Catalog(
        specs={
            "a-class": _composite_spec("a-class", "b-class"),
            "b-class": _composite_spec("b-class", "a-class"),
        }
    )
    with pytest.raises(RecoverableError, match="cycle"):
        await runtime.create_from_catalog(catalog, pools=PoolsConfig(), spec_yamls={})
    assert await runtime.list_classes() == []


@pytest.mark.asyncio
async def test_boot_skips_unsatisfied_dependency_but_records_disabled(
    beaver_db,
) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    catalog = Catalog(specs={"one-class": _composite_spec("one-class", "two-class")})
    try:
        await runtime.create_from_catalog(catalog, pools=PoolsConfig(), spec_yamls=_spec_yamls())
        assert await runtime.class_state("one-class") == ActivityState.DISABLED
    finally:
        await _teardown(beaver_db, runtime, ["one-class"], pump=pump)


@pytest.mark.asyncio
async def test_boot_unknown_spec_in_yamls_is_ignored(beaver_db) -> None:
    runtime = _runtime(beaver_db)
    pump = _start_executor(runtime)
    catalog = Catalog(specs={"two-class": _atomic_spec("two-class")})
    try:
        await runtime.create_from_catalog(
            catalog,
            pools=PoolsConfig(),
            spec_yamls={"two-class": _atomic_yaml("two-class"), "ghost": "name: ghost"},
        )
        assert await runtime.class_state("two-class") == ActivityState.ENABLED
    finally:
        await _teardown(beaver_db, runtime, ["two-class"], pump=pump)
