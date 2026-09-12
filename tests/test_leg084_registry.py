"""Contract tests for LEG-084 — the Registry: posterior mirror of the catalog.

Written red-first per AGENTS.md rule 5. The Registry (``legio.registry``)
records facts *after* they happen (registration-is-a-mirror), never before: the
live catalog (classes / instances / dependencies) and the runtime YAML cache
(AGENT_LIFECYCLE §0/§4.7/§4.8). It never initiates or materializes anything.
"""

from __future__ import annotations

import logging
import re
from typing import Any, cast

import pytest
from beaver import AsyncBeaverDB

from legio.patterns.schema1 import AgentKind
from legio.registry import ActivityState, Registry

logger = logging.getLogger("tests.test_leg084_registry")

_CATALOG_SCOPE = "catalog"
_INSTANCES_SCOPE = "instances"
_YAML_CACHE_SCOPE = "yaml_cache"

_CLASS_QUEUE = "some-queue"


def _registry(beaver_db: AsyncBeaverDB) -> Registry:
    return Registry(beaver_db)


def _queue_key(agent_id: str) -> str:
    return f"legio:queue:{agent_id}"


async def _raw(db: AsyncBeaverDB, scope: str) -> dict[str, Any]:
    """Read a beaver scope as a plain (key → record) map.

    Values are the stored JSON data as-is (a dict for catalog/instances, a
    plain string for ``yaml_cache``) — never re-wrapped.
    """
    d = db.dict(scope)
    items: dict[str, Any] = {}
    async for key, value in d.items():
        items[key] = value if isinstance(value, str) else dict(value)
    return items


@pytest.mark.asyncio
async def test_registry_requires_a_connected_beaver_db() -> None:
    with pytest.raises(TypeError):
        Registry(cast(AsyncBeaverDB, None))


@pytest.mark.asyncio
async def test_scope_footprint_is_exactly_catalog_instances_yaml_cache(
    beaver_db: AsyncBeaverDB,
) -> None:
    registry = _registry(beaver_db)
    await registry.record_class(
        "one-class",
        AgentKind.TOOL,
        dependencies=[],
        queue=_CLASS_QUEUE,
        state=ActivityState.ENABLED,
    )
    await registry.cache_spec("one-class", "spec: one")
    await registry.record_instance("one-class", "first-agent", state=ActivityState.ENABLED)
    await registry.record_instance("one-class", "second-agent", state=ActivityState.ENABLED)

    assert await _raw(beaver_db, _CATALOG_SCOPE) != {}
    assert await _raw(beaver_db, _INSTANCES_SCOPE) != {}
    assert await _raw(beaver_db, _YAML_CACHE_SCOPE) != {}


@pytest.mark.asyncio
async def test_record_class_creates_a_class_record(beaver_db: AsyncBeaverDB) -> None:
    registry = _registry(beaver_db)
    await registry.record_class(
        "one-class",
        AgentKind.TOOL,
        dependencies=["dep-a", "dep-b"],
        queue=_CLASS_QUEUE,
        state=ActivityState.ENABLED,
    )

    raw = (await _raw(beaver_db, _CATALOG_SCOPE))["one-class"]
    assert raw["name"] == "one-class"
    assert raw["kind"] == "tool"
    assert raw["state"] == "enabled"
    assert raw["queue"] == _CLASS_QUEUE
    assert raw["dependencies"] == ["dep-a", "dep-b"]


@pytest.mark.asyncio
async def test_record_class_kind_binding_none_means_composite(
    beaver_db: AsyncBeaverDB,
) -> None:
    registry = _registry(beaver_db)
    await registry.record_class(
        "composite-class",
        None,
        dependencies=["branch-a", "branch-b"],
        queue="composite-queue",
        state=ActivityState.ENABLED,
    )

    raw = (await _raw(beaver_db, _CATALOG_SCOPE))["composite-class"]
    assert raw["kind"] is None


@pytest.mark.asyncio
async def test_record_class_rejects_unknown_kind_loudly(
    beaver_db: AsyncBeaverDB,
) -> None:
    registry = _registry(beaver_db)
    with pytest.raises((TypeError, ValueError)):
        await registry.record_class(
            "bad-class",
            "science",  # type: ignore[arg-type]  # not an AgentKind
            dependencies=[],
            queue=_CLASS_QUEUE,
            state=ActivityState.ENABLED,
        )
    assert await _raw(beaver_db, _CATALOG_SCOPE) == {}


@pytest.mark.asyncio
async def test_record_class_duplicate_is_a_noop(beaver_db: AsyncBeaverDB) -> None:
    registry = _registry(beaver_db)
    await registry.record_class(
        "one-class",
        AgentKind.TOOL,
        dependencies=[],
        queue=_CLASS_QUEUE,
        state=ActivityState.ENABLED,
    )
    await registry.record_class(
        "one-class",
        AgentKind.TOOL,
        dependencies=[],
        queue=_CLASS_QUEUE,
        state=ActivityState.ENABLED,
    )

    await registry.record_instance("one-class", "first-agent", state=ActivityState.ENABLED)
    records = await _raw(beaver_db, _CATALOG_SCOPE)
    assert list(records.keys()) == ["one-class"]


@pytest.mark.asyncio
async def test_cache_spec_is_an_upsert_over_the_yaml_cache(
    beaver_db: AsyncBeaverDB,
) -> None:
    registry = _registry(beaver_db)
    await registry.cache_spec("one-class", "first-version")
    await registry.cache_spec("one-class", "second-version")

    assert await registry.get_cached_spec("one-class") == "second-version"
    assert await registry.get_cached_spec("never-cached") is None


@pytest.mark.asyncio
async def test_record_instance_never_precedes_its_class(
    beaver_db: AsyncBeaverDB,
) -> None:
    """No orphan instances: recording an instance of an unknown class errors."""
    registry = _registry(beaver_db)
    with pytest.raises(KeyError):
        await registry.record_instance("nowhere-class", "first-agent", state=ActivityState.ENABLED)
    assert await _raw(beaver_db, _INSTANCES_SCOPE) == {}


@pytest.mark.asyncio
async def test_ordered_create_mirror_chain_is_recorded(
    beaver_db: AsyncBeaverDB,
) -> None:
    registry = _registry(beaver_db)
    await registry.record_class(
        "one-class",
        AgentKind.TOOL,
        dependencies=[],
        queue=_CLASS_QUEUE,
        state=ActivityState.ENABLED,
    )
    await registry.cache_spec("one-class", "spec: one")
    await registry.record_instance("one-class", "first-agent", state=ActivityState.ENABLED)

    raw = (await _raw(beaver_db, _INSTANCES_SCOPE))["one-class:first-agent"]
    assert raw["class_name"] == "one-class"
    assert raw["instance_id"] == "first-agent"
    assert raw["state"] == "enabled"
    assert set((await _raw(beaver_db, _INSTANCES_SCOPE)).keys()) == {"one-class:first-agent"}


@pytest.mark.asyncio
async def test_record_instance_duplicate_is_a_noop(beaver_db: AsyncBeaverDB) -> None:
    registry = _registry(beaver_db)
    await registry.record_class(
        "one-class",
        AgentKind.TOOL,
        dependencies=[],
        queue=_CLASS_QUEUE,
        state=ActivityState.ENABLED,
    )
    await registry.record_instance("one-class", "first-agent", state=ActivityState.ENABLED)
    await registry.record_instance("one-class", "first-agent", state=ActivityState.ENABLED)

    assert list((await _raw(beaver_db, _INSTANCES_SCOPE)).keys()) == ["one-class:first-agent"]


@pytest.mark.asyncio
async def test_effective_state_is_enabled_only_with_instances(
    beaver_db: AsyncBeaverDB,
) -> None:
    """§4.4: a stored-enabled class with zero recorded instances reads disabled."""
    registry = _registry(beaver_db)

    await registry.record_class(
        "one-class",
        AgentKind.TOOL,
        dependencies=[],
        queue=_CLASS_QUEUE,
        state=ActivityState.ENABLED,
    )
    assert await registry.class_state("one-class") == ActivityState.DISABLED
    assert all(item.state == ActivityState.DISABLED for item in await registry.list_classes())

    await registry.record_instance("one-class", "first-agent", state=ActivityState.ENABLED)
    assert await registry.class_state("one-class") == ActivityState.ENABLED


@pytest.mark.asyncio
async def test_set_class_state_on_unknown_class_is_an_error(
    beaver_db: AsyncBeaverDB,
) -> None:
    registry = _registry(beaver_db)
    with pytest.raises(KeyError):
        await registry.set_class_state("nowhere-class", ActivityState.DISABLED)


@pytest.mark.asyncio
async def test_set_instance_state_on_unknown_instance_is_an_error(
    beaver_db: AsyncBeaverDB,
) -> None:
    registry = _registry(beaver_db)
    await registry.record_class(
        "one-class",
        AgentKind.TOOL,
        dependencies=[],
        queue=_CLASS_QUEUE,
        state=ActivityState.ENABLED,
    )
    with pytest.raises(KeyError):
        await registry.set_instance_state("one-class", "ghost-agent", ActivityState.DISABLED)


@pytest.mark.asyncio
async def test_set_class_state_records_enable_disable(
    beaver_db: AsyncBeaverDB,
) -> None:
    registry = _registry(beaver_db)
    await registry.record_class(
        "one-class",
        AgentKind.TOOL,
        dependencies=[],
        queue=_CLASS_QUEUE,
        state=ActivityState.ENABLED,
    )
    await registry.record_instance("one-class", "first-agent", state=ActivityState.ENABLED)
    assert await registry.class_state("one-class") == ActivityState.ENABLED

    await registry.set_class_state("one-class", ActivityState.DISABLED)
    assert await registry.class_state("one-class") == ActivityState.DISABLED

    await registry.set_class_state("one-class", ActivityState.ENABLED)
    assert await registry.class_state("one-class") == ActivityState.ENABLED


@pytest.mark.asyncio
async def test_set_instance_state_records_enable_disable(
    beaver_db: AsyncBeaverDB,
) -> None:
    registry = _registry(beaver_db)
    await registry.record_class(
        "one-class",
        AgentKind.TOOL,
        dependencies=[],
        queue=_CLASS_QUEUE,
        state=ActivityState.ENABLED,
    )
    await registry.record_instance("one-class", "first-agent", state=ActivityState.DISABLED)
    first = await registry.get_instance("one-class", "first-agent")
    assert first is not None and first.state == ActivityState.DISABLED

    await registry.set_instance_state("one-class", "first-agent", ActivityState.ENABLED)
    second = await registry.get_instance("one-class", "first-agent")
    assert second is not None and second.state == ActivityState.ENABLED


@pytest.mark.asyncio
async def test_class_dependencies_returns_direct_dependencies(
    beaver_db: AsyncBeaverDB,
) -> None:
    registry = _registry(beaver_db)
    await registry.record_class(
        "top-class",
        AgentKind.TOOL,
        dependencies=["mid-a", "mid-b"],
        queue=_CLASS_QUEUE,
        state=ActivityState.ENABLED,
    )
    assert await registry.class_dependencies("top-class") == ["mid-a", "mid-b"]
    with pytest.raises(KeyError):
        await registry.class_dependencies("nowhere-class")


@pytest.mark.asyncio
async def test_class_dependents_direct_and_transitive(
    beaver_db: AsyncBeaverDB,
) -> None:
    registry = _registry(beaver_db)
    await registry.record_class(
        "leaf-a",
        AgentKind.TOOL,
        dependencies=[],
        queue=_CLASS_QUEUE,
        state=ActivityState.ENABLED,
    )
    await registry.record_class(
        "leaf-b",
        AgentKind.TOOL,
        dependencies=[],
        queue=_CLASS_QUEUE,
        state=ActivityState.ENABLED,
    )
    await registry.record_class(
        "mid-a",
        AgentKind.TOOL,
        dependencies=["leaf-a"],
        queue=_CLASS_QUEUE,
        state=ActivityState.ENABLED,
    )
    await registry.record_class(
        "mid-b",
        AgentKind.TOOL,
        dependencies=["leaf-a", "leaf-b"],
        queue=_CLASS_QUEUE,
        state=ActivityState.ENABLED,
    )
    await registry.record_class(
        "top-class",
        AgentKind.TOOL,
        dependencies=["mid-a", "mid-b"],
        queue=_CLASS_QUEUE,
        state=ActivityState.ENABLED,
    )

    assert set(await registry.class_dependents("leaf-a")) == {"mid-a", "mid-b"}
    assert set(await registry.class_dependents("leaf-b")) == {"mid-b"}
    assert set(await registry.class_dependents("leaf-a", transitive=True)) == {
        "mid-a",
        "mid-b",
        "top-class",
    }
    assert set(await registry.class_dependents("nowhere-class")) == set()


@pytest.mark.asyncio
async def test_dependencies_satisfied_read_helper(beaver_db: AsyncBeaverDB) -> None:
    registry = _registry(beaver_db)
    await registry.record_class(
        "leaf",
        AgentKind.TOOL,
        dependencies=[],
        queue=_CLASS_QUEUE,
        state=ActivityState.ENABLED,
    )
    await registry.record_class(
        "root",
        AgentKind.TOOL,
        dependencies=["leaf"],
        queue=_CLASS_QUEUE,
        state=ActivityState.ENABLED,
    )

    await registry.record_instance("leaf", "leaf-agent", state=ActivityState.ENABLED)
    await registry.record_instance("root", "root-agent", state=ActivityState.ENABLED)

    assert await registry.dependencies_satisfied("root") is True

    await registry.set_class_state("leaf", ActivityState.DISABLED)
    assert await registry.dependencies_satisfied("root") is False

    await registry.set_class_state("leaf", ActivityState.ENABLED)
    assert await registry.dependencies_satisfied("root") is True


@pytest.mark.asyncio
async def test_list_instances_and_get_instance(beaver_db: AsyncBeaverDB) -> None:
    registry = _registry(beaver_db)
    await registry.record_class(
        "one-class",
        AgentKind.TOOL,
        dependencies=[],
        queue=_CLASS_QUEUE,
        state=ActivityState.ENABLED,
    )
    await registry.record_instance("one-class", "first-agent", state=ActivityState.ENABLED)
    await registry.record_instance("one-class", "second-agent", state=ActivityState.DISABLED)

    instances = await registry.list_instances("one-class")
    by_id = {i.instance_id: i.state for i in instances}
    assert by_id == {
        "first-agent": ActivityState.ENABLED,
        "second-agent": ActivityState.DISABLED,
    }

    got = await registry.get_instance("one-class", "first-agent")
    assert got is not None and got.instance_id == "first-agent"
    assert await registry.get_instance("one-class", "ghost-agent") is None
    assert await registry.list_instances("nowhere-class") == []


@pytest.mark.asyncio
async def test_remove_instance_and_last_instance_disables_effectively(
    beaver_db: AsyncBeaverDB,
) -> None:
    registry = _registry(beaver_db)
    await registry.record_class(
        "one-class",
        AgentKind.TOOL,
        dependencies=[],
        queue=_CLASS_QUEUE,
        state=ActivityState.ENABLED,
    )
    await registry.record_instance("one-class", "first-agent", state=ActivityState.ENABLED)
    await registry.record_instance("one-class", "second-agent", state=ActivityState.ENABLED)

    await registry.remove_instance("one-class", "first-agent")
    remaining = await registry.list_instances("one-class")
    assert [i.instance_id for i in remaining] == ["second-agent"]

    await registry.remove_instance("one-class", "second-agent")
    assert await registry.class_state("one-class") == ActivityState.DISABLED

    await registry.remove_instance("one-class", "ghost-agent")


@pytest.mark.asyncio
async def test_remove_class_keeps_the_yaml_cache_but_removes_catalog(
    beaver_db: AsyncBeaverDB,
) -> None:
    registry = _registry(beaver_db)
    await registry.record_class(
        "one-class",
        AgentKind.TOOL,
        dependencies=[],
        queue=_CLASS_QUEUE,
        state=ActivityState.ENABLED,
    )
    await registry.cache_spec("one-class", "spec: one")
    await registry.record_instance("one-class", "first-agent", state=ActivityState.ENABLED)

    await registry.remove_class("one-class")

    assert await registry.class_state("one-class") is None
    assert await registry.list_instances("one-class") == []
    assert await registry.get_cached_spec("one-class") == "spec: one"

    await registry.remove_class("nowhere-class")


@pytest.mark.asyncio
async def test_remove_class_removes_all_instances_armageddon(
    beaver_db: AsyncBeaverDB,
) -> None:
    """§4.5: destroying a class removes the spec and all its instances."""
    registry = _registry(beaver_db)
    await registry.record_class(
        "one-class",
        AgentKind.TOOL,
        dependencies=[],
        queue=_CLASS_QUEUE,
        state=ActivityState.ENABLED,
    )
    await registry.record_instance("one-class", "first-agent", state=ActivityState.ENABLED)
    await registry.record_instance("one-class", "second-agent", state=ActivityState.ENABLED)

    await registry.remove_class("one-class")

    assert await _raw(beaver_db, _INSTANCES_SCOPE) == {}


@pytest.mark.asyncio
async def test_class_state_unknown_is_none(beaver_db: AsyncBeaverDB) -> None:
    registry = _registry(beaver_db)
    assert await registry.class_state("nowhere-class") is None
    assert await registry.list_classes() == []


@pytest.mark.asyncio
async def test_registry_logs_records_and_denials(beaver_db: AsyncBeaverDB, caplog) -> None:
    registry = _registry(beaver_db)
    with caplog.at_level(logging.INFO, logger="legio.registry"):
        await registry.record_class(
            "one-class",
            AgentKind.TOOL,
            dependencies=[],
            queue=_CLASS_QUEUE,
            state=ActivityState.ENABLED,
        )
        await registry.record_instance("one-class", "first-agent", state=ActivityState.ENABLED)
    assert any("record_class" in r.message for r in caplog.records)


def test_activity_state_values() -> None:
    assert ActivityState.ENABLED.value == "enabled"
    assert ActivityState.DISABLED.value == "disabled"


def test_instance_key_namespace_separates_classes() -> None:
    assert _INSTANCES_SCOPE == "instances"
    assert re.fullmatch(r"one-class:first-agent", "one-class:first-agent")
