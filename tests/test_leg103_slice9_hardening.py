"""Contract tests for LEG-103 Slice 9 — fresh-audit hardening.

Strict numerics at every budget seam, validate-before-mutate destroy,
loser slot cleanup on a lost close, cancel-safe drain-kick coalescing,
and named failure shapes for unreadable pattern files.
"""

from __future__ import annotations

import asyncio

import pytest
from beaver import AsyncBeaverDB
from pydantic import ValidationError

from legio.agents.composite_agent import CompositeAgent
from legio.cli import _collect_spec_yamls
from legio.config import (
    EnvSecrets,
    LegioConfig,
    LifecycleParams,
    LoadedConfig,
    PatternsConfig,
    PoolsConfig,
    load_tools_file,
)
from legio.errors import ConfigError, RecoverableError
from legio.manager import Manager
from legio.registry import ActivityState
from legio.runtime import Runtime
from tests.test_leg041_multi_branch_composite import (
    GatherComposite,
    child_result,
    composite_request,
)
from tests.test_leg085_runtime import _atomic_yaml, _load_atomic_spec


def write_tools_yaml(path, body: str):
    tools_file = path / "tools.yaml"
    tools_file.write_text(body, encoding="utf-8")
    return tools_file


def test_tool_timeout_bool_rejected_at_load(tmp_path) -> None:
    """Slice 9 (F1): pydantic coerces `True -> 1` before after-validators —
    booleans are rejected explicitly instead of becoming a silent 1 s timeout."""
    tools_file = write_tools_yaml(
        tmp_path,
        "available_tools:\n"
        "  bad:\n"
        "    implementation: tests.test_tools.fake_transform\n"
        "    policy: {timeout: true, retries: 0}\n",
    )
    with pytest.raises(ConfigError):
        load_tools_file(tools_file)


def test_tool_retries_bool_rejected_at_load(tmp_path) -> None:
    """Slice 9 (F1): `retries: true` is an authoring error at load (distinct
    from Slice 1's nonzero-int case, which still fails loudly at execution)."""
    tools_file = write_tools_yaml(
        tmp_path,
        "available_tools:\n"
        "  bad:\n"
        "    implementation: tests.test_tools.fake_transform\n"
        "    policy: {timeout: 30, retries: true}\n",
    )
    with pytest.raises(ConfigError):
        load_tools_file(tools_file)


@pytest.mark.parametrize("field", ["drain_timeout", "drain_interval"])
def test_lifecycle_bool_budgets_rejected(field: str) -> None:
    """Slice 9 (F2): a bool drain budget must not coerce silently to 1.0."""
    with pytest.raises(ValidationError):
        LifecycleParams(**{field: True})


def test_gather_budget_bool_rejected(beaver_db: AsyncBeaverDB) -> None:
    """Slice 9 (F2): `gather_budget=True` passes `isfinite`/`<= 0` — rejected
    explicitly like every other budget seam."""
    with pytest.raises(ValueError, match="gather_budget"):
        CompositeAgent(
            agent_id="comp",
            db=beaver_db,
            branches=[[("b1", "b1")]],
            gather_budget=True,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("value", [True, "3", 1.0])
def test_pool_sizes_require_genuine_ints(value) -> None:
    """Slice 9 (F3): pool intent misconfigures capacity silently on coercion —
    only genuine ints pass."""
    with pytest.raises(ValidationError):
        PoolsConfig(per_pattern={"a": value})
    with pytest.raises(ValidationError):
        PoolsConfig(default=value)


def test_pool_genuine_ints_still_pass() -> None:
    """Guard: real pool intent (including born-disabled 0) is untouched."""
    pools = PoolsConfig(per_pattern={"a": 2}, default=0)
    assert pools.resolve("a", per_kind=None) == 2
    assert pools.resolve("other", per_kind=None) == 0


@pytest.mark.parametrize("ttl", [float("nan"), float("inf"), -1.0, True])
def test_manager_control_ttl_validated_at_construction(beaver_db: AsyncBeaverDB, ttl) -> None:
    """Slice 9 (F4): the fourth budget seam requires a finite number > 0."""
    with pytest.raises(ValueError, match="control_ttl"):
        Manager(beaver_db, node_id="slice9@host", control_ttl=ttl)


def test_manager_default_control_ttl_ok(beaver_db: AsyncBeaverDB) -> None:
    """Guard: default construction is untouched."""
    assert Manager(beaver_db, node_id="slice9@host")._control_ttl == 60.0


@pytest.mark.asyncio
async def test_destroy_bogus_mode_leaves_gate_open(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Slice 9 (F5): a failed destroy (unknown mode) must not close the gate —
    validate before mutating."""

    async def _pump(runtime: Runtime) -> None:
        while True:
            await runtime.manager.run()
            await asyncio.sleep(0)

    runtime = Runtime(beaver_db, node_id="slice9@host")
    pump = asyncio.create_task(_pump(runtime))
    try:
        name, spec = _load_atomic_spec("doomed")
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("doomed"), pool=1)
        with pytest.raises(ValueError, match="bogus"):
            await runtime.destroy_class(name, mode="bogus")  # type: ignore[arg-type]
        gate = await runtime._gates.fetch(name)
        assert gate is None or gate.get("state") != ActivityState.DISABLED.value
        assert await runtime.class_state(name) == ActivityState.ENABLED
    finally:
        try:
            await runtime.destroy_class("doomed", mode="now")
        except RecoverableError as exc:  # teardown is best effort; stay visible
            print(f"teardown skipped class=doomed: {exc}")
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)


@pytest.mark.asyncio
async def test_lost_close_deletes_only_loser_slot(
    beaver_db: AsyncBeaverDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 9 (F6): the atomic-close loser removes the slot key it just wrote
    (best-effort) and leaves the winner's record and other slots intact."""
    comp = GatherComposite(
        agent_id="comp",
        db=beaver_db,
        branches=[[("b1", "b1")], [("b2", "b2")]],
        input_as="main",
        output_as="comp",
    )
    task_id = "T-loserslot"
    request = composite_request(
        task_id=task_id,
        payload={"main": {"seed": 1}},
        route=(("main", "main"), ("comp", "comp"), ("after", "after")),
        current_index=1,
        end_of_level_queue="result:" + task_id,
    )
    await comp._fan_out(request)
    record = await comp._state.fetch(task_id)
    first_branch, second_branch = list(record["expected"])
    await comp._slots.set(f"{task_id}:{second_branch}", {"index": 1, "result": {"b2": 2}})

    async def losing_delete(key: str):
        raise KeyError(key)

    monkeypatch.setattr(comp._state, "delete", losing_delete)
    result = child_result(
        branch_id=first_branch,
        level_route=(("b1", "b1"),),
        task_id=task_id,
        payload={"b1": 1},
    )
    await comp._fan_in(result)

    assert await comp._slots.fetch(f"{task_id}:{first_branch}") is None
    assert await comp._state.fetch(task_id) is not None
    assert await comp._slots.fetch(f"{task_id}:{second_branch}") is not None


@pytest.mark.asyncio
async def test_cancelled_kick_submit_clears_coalesce_flag(
    beaver_db: AsyncBeaverDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 9 (F7): cancellation is a BaseException — the coalesce flag must
    still clear while the cancellation propagates."""

    async def cancelled_submit(*args, **kwargs):
        raise asyncio.CancelledError()

    runtime = Runtime(beaver_db, node_id="slice9@host")
    monkeypatch.setattr(runtime.manager, "submit_task", cancelled_submit)
    with pytest.raises(asyncio.CancelledError):
        await runtime._kick_result_drain("cancelled_agent")
    assert "cancelled_agent" not in runtime._drain_inflight


def _pattern_dirs(tmp_path):
    tool_dir = tmp_path / "tool"
    tool_dir.mkdir()
    ling_dir = tmp_path / "linguistic"
    ling_dir.mkdir()
    comp_dir = tmp_path / "composite"
    comp_dir.mkdir()
    return LoadedConfig(
        config=LegioConfig(
            patterns=PatternsConfig(tool=tool_dir, linguistic=ling_dir, composite=comp_dir)
        ),
        secrets=EnvSecrets(),
        config_path=None,
    )


def test_yaml_cache_collection_names_undecodable_file(tmp_path) -> None:
    """Slice 9 (F11): non-UTF8 bytes are a ValueError, not a YAMLError — still
    a ConfigError naming the file."""
    loaded = _pattern_dirs(tmp_path)
    bad = tmp_path / "tool" / "binary.yaml"
    bad.write_bytes(b"name: \xff\xfe\n")
    with pytest.raises(ConfigError) as excinfo:
        _collect_spec_yamls(loaded)
    assert "binary.yaml" in str(excinfo.value)


def test_yaml_cache_collection_names_unreadable_file(tmp_path) -> None:
    """Slice 9 (F11): an OSError (e.g. a directory named *.yaml) is still a
    ConfigError naming the path."""
    loaded = _pattern_dirs(tmp_path)
    (tmp_path / "tool" / "dir.yaml").mkdir()
    with pytest.raises(ConfigError) as excinfo:
        _collect_spec_yamls(loaded)
    assert "dir.yaml" in str(excinfo.value)
