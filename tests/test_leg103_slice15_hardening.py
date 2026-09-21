"""Contract tests for LEG-103 Slice 15 — ninth-audit hardening.

Loader shape rejections log (F1), verb-entry denials log (F2),
index-divergence fallback everywhere (F3). F4/F5/N6 are docs-only.
"""

from __future__ import annotations

import logging

import pytest
import yaml
from beaver import AsyncBeaverDB
from pydantic import ValidationError

from legio.cli import _as_int
from legio.errors import UnrecoverableError
from legio.naming import ActivityState, queue_key
from legio.patterns import load_patterns
from legio.registry import Registry
from legio.runtime import Runtime
from tests.test_leg085_runtime import _atomic_yaml, _load_atomic_spec


def _atom_dict(name: str) -> dict:
    return yaml.safe_load(_atomic_yaml(name))


# --------------------------------------------------------------------------
# F1 — loader schema-shape rejections log
# --------------------------------------------------------------------------


def test_bad_shape_pattern_logs_and_wraps(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Slice 15 (F1): a schema-shape rejection logs and raises
    UnrecoverableError — never a raw ValidationError."""
    bad = _atom_dict("badshape")
    bad["kind"] = "bogus-kind"
    with (
        caplog.at_level(logging.WARNING, logger="legio.patterns.loader"),
        pytest.raises(UnrecoverableError) as excinfo,
    ):
        load_patterns([bad])
    assert not isinstance(excinfo.value, ValidationError)
    assert "badshape" in caplog.text


def test_non_mapping_document_still_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Slice 15 (F1): the shape wrap covers every construction failure."""
    with (
        caplog.at_level(logging.WARNING, logger="legio.patterns.loader"),
        pytest.raises(UnrecoverableError),
    ):
        load_patterns([{"name": "noname"}])
    assert "patterns reject" in caplog.text


# --------------------------------------------------------------------------
# F2 — verb-entry denials log
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_empty_route_logs(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture
) -> None:
    """Slice 15 (F2): empty-route submit logs naming the verb."""
    runtime = Runtime(beaver_db, node_id="slice15@host")
    with (
        caplog.at_level(logging.WARNING, logger="legio.runtime"),
        pytest.raises(ValueError, match="route"),
    ):
        await runtime.submit("c", (), {})
    assert "submit" in caplog.text


@pytest.mark.asyncio
async def test_submit_work_item_empty_route_logs(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture
) -> None:
    """Slice 15 (F2): empty-route work-item logs naming the verb."""
    runtime = Runtime(beaver_db, node_id="slice15@host")
    with (
        caplog.at_level(logging.WARNING, logger="legio.runtime"),
        pytest.raises(ValueError, match="route"),
    ):
        await runtime.submit_work_item("peer", (), {}, task_id="peer:t")
    assert "work_item" in caplog.text


@pytest.mark.asyncio
async def test_create_class_bad_pool_logs(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture
) -> None:
    """Slice 15 (F2): bad-pool create logs naming the verb and value."""
    runtime = Runtime(beaver_db, node_id="slice15@host")
    _name, spec = _load_atomic_spec("badpool")
    with (
        caplog.at_level(logging.WARNING, logger="legio.runtime"),
        pytest.raises(ValueError, match="pool"),
    ):
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("badpool"), pool=-1)
    assert "create_class" in caplog.text


@pytest.mark.asyncio
async def test_destroy_bad_mode_logs(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture
) -> None:
    """Slice 15 (F2): bogus destroy mode logs naming the mode."""
    runtime = Runtime(beaver_db, node_id="slice15@host")
    with (
        caplog.at_level(logging.WARNING, logger="legio.runtime"),
        pytest.raises(ValueError, match="mode"),
    ):
        await runtime.destroy_class("any", mode="bogus")  # type: ignore[arg-type]
    assert "bogus" in caplog.text


@pytest.mark.asyncio
async def test_create_instance_bad_count_logs(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture
) -> None:
    """Slice 15 (F2): bad-count create logs naming the verb."""
    runtime = Runtime(beaver_db, node_id="slice15@host")
    with (
        caplog.at_level(logging.WARNING, logger="legio.runtime"),
        pytest.raises(ValueError, match="count"),
    ):
        await runtime.create_instance("any", count=0)
    assert "create_instance" in caplog.text


def test_as_int_rejection_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Slice 15 (F2): non-int CLI option logs naming the value."""
    with (
        caplog.at_level(logging.WARNING, logger="legio.cli"),
        pytest.raises(ValueError, match="genuine integer"),
    ):
        _as_int("three", 1)
    assert "three" in caplog.text


# --------------------------------------------------------------------------
# F3 — index-divergence fallback everywhere
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_instances_heals_with_warning(
    beaver_db: AsyncBeaverDB,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slice 15 (F3): stored rows absent from the index are still listed
    (scan fallback) with a warning — never silently dropped."""
    registry = Registry(beaver_db)
    name, spec = _load_atomic_spec("listed")
    await registry.record_class(
        name,
        spec.kind,
        dependencies=[],
        queue=queue_key(name),
        state=ActivityState.ENABLED,
    )
    await registry.record_instance(name, "i-0", state=ActivityState.ENABLED)
    await registry._instances_by_class.delete(name)

    iters = 0
    real_instances = registry._instances

    class _SpyDict:
        def __init__(self, inner):  # type: ignore[no-untyped-def]
            self._inner = inner

        def __getattr__(self, attr):  # type: ignore[no-untyped-def]
            return getattr(self._inner, attr)

        async def __aiter__(self):  # type: ignore[no-untyped-def]
            nonlocal iters
            iters += 1
            async for key in self._inner:
                yield key

    monkeypatch.setattr(registry, "_instances", _SpyDict(real_instances))
    with caplog.at_level(logging.WARNING, logger="legio.registry"):
        listed = await registry.list_instances(name)
    assert [record.instance_id for record in listed] == ["i-0"]
    assert "index miss" in caplog.text
    assert iters == 1


@pytest.mark.asyncio
async def test_list_instances_fast_path_never_scans(
    beaver_db: AsyncBeaverDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 15 (F3): a populated index never touches the instances scope."""
    registry = Registry(beaver_db)
    name, spec = _load_atomic_spec("fastpath")
    await registry.record_class(
        name,
        spec.kind,
        dependencies=[],
        queue=queue_key(name),
        state=ActivityState.ENABLED,
    )
    await registry.record_instance(name, "i-0", state=ActivityState.ENABLED)

    iters = 0
    real_instances = registry._instances

    class _SpyDict:
        def __init__(self, inner):  # type: ignore[no-untyped-def]
            self._inner = inner

        def __getattr__(self, attr):  # type: ignore[no-untyped-def]
            return getattr(self._inner, attr)

        async def __aiter__(self):  # type: ignore[no-untyped-def]
            nonlocal iters
            iters += 1
            async for key in self._inner:
                yield key

    monkeypatch.setattr(registry, "_instances", _SpyDict(real_instances))
    listed = await registry.list_instances(name)
    assert [record.instance_id for record in listed] == ["i-0"]
    assert iters == 0


@pytest.mark.asyncio
async def test_remove_class_heals_with_warning(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture
) -> None:
    """Slice 15 (F3): remove on a diverged index still deletes the stored
    rows (scan fallback) with a warning — no orphan rows left."""
    registry = Registry(beaver_db)
    name, spec = _load_atomic_spec("doomed")
    await registry.record_class(
        name,
        spec.kind,
        dependencies=[],
        queue=queue_key(name),
        state=ActivityState.ENABLED,
    )
    await registry.record_instance(name, "i-0", state=ActivityState.ENABLED)
    await registry._instances_by_class.delete(name)
    with caplog.at_level(logging.WARNING, logger="legio.registry"):
        await registry.remove_class(name)
    assert f"{name}:i-0" not in [key async for key in registry._instances]
    assert "index miss" in caplog.text
