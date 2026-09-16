"""Contract tests for LEG-103 Slice 8 — post-re-audit hardening.

Covers the necessary fixes from the Session 101 leftovers: validated tool
timeouts (load + direct-registry execution path), finite budget seams,
a named failure shape for YAML-cache collection, coalesce-flag hygiene on a
failed drain-kick submit, and loser cleanup on a lost fan-out race.
"""

from __future__ import annotations

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
    load_tools_file,
)
from legio.errors import ConfigError
from legio.runtime import Runtime
from tests.test_leg022_toolagent import _run_tool_case, _tool_agent
from tests.test_leg041_multi_branch_composite import GatherComposite, composite_request


def write_tools_yaml(path, body: str):
    """Write a Schema 3 tools file with the given YAML body."""
    tools_file = path / "tools.yaml"
    tools_file.write_text(body, encoding="utf-8")
    return tools_file


def test_negative_tool_timeout_rejected_at_load(tmp_path) -> None:
    """Slice 8 (m7): a negative per-call timeout is an authoring error."""
    tools_file = write_tools_yaml(
        tmp_path,
        "available_tools:\n"
        "  bad:\n"
        "    implementation: tests.test_tools.fake_transform\n"
        "    policy: {timeout: -5, retries: 0}\n",
    )
    with pytest.raises(ConfigError):
        load_tools_file(tools_file)


def test_zero_tool_timeout_rejected_at_load(tmp_path) -> None:
    """Slice 8 (m7): zero would time out every call at once — refused."""
    tools_file = write_tools_yaml(
        tmp_path,
        "available_tools:\n"
        "  bad:\n"
        "    implementation: tests.test_tools.fake_transform\n"
        "    policy: {timeout: 0, retries: 0}\n",
    )
    with pytest.raises(ConfigError):
        load_tools_file(tools_file)


def test_nan_tool_timeout_rejected_at_load(tmp_path) -> None:
    """Slice 8 (m7/NaN): NaN slips through every `<= 0` check — refused."""
    tools_file = write_tools_yaml(
        tmp_path,
        "available_tools:\n"
        "  bad:\n"
        "    implementation: tests.test_tools.fake_transform\n"
        "    policy: {timeout: .nan, retries: 0}\n",
    )
    with pytest.raises(ConfigError):
        load_tools_file(tools_file)


def test_negative_retries_rejected_at_load_but_nonzero_passes(tmp_path) -> None:
    """Slice 8 (m7): negative retries are meaningless (load error); a positive
    value still passes load so Slice 1 can fail it loudly at execution."""
    negative = write_tools_yaml(
        tmp_path,
        "available_tools:\n"
        "  bad:\n"
        "    implementation: tests.test_tools.fake_transform\n"
        "    policy: {timeout: 30, retries: -1}\n",
    )
    with pytest.raises(ConfigError):
        load_tools_file(negative)

    loud_later = write_tools_yaml(
        tmp_path,
        "available_tools:\n"
        "  retrying:\n"
        "    implementation: tests.test_tools.fake_transform\n"
        "    policy: {timeout: 30, retries: 1}\n",
    )
    assert "retrying" in load_tools_file(loud_later).available_tools


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [-5, 0, "soon"])
async def test_direct_registry_bad_timeout_fails_loudly(
    beaver_db: AsyncBeaverDB, timeout
) -> None:
    """Slice 8 (m7): the direct-registry path bypasses file validation, so a
    meaningless timeout fails at execution naming the policy (never a bare
    immediate TimeoutError or a raw float() crash)."""
    agent, request = _tool_agent(
        beaver_db,
        task_id="T-badtimeout",
        tool_name="transform",
        implementation="tests.test_tools.fake_transform",
        policy={"timeout": timeout, "retries": 0},
    )
    payload = await _run_tool_case(beaver_db, agent, request)
    assert "policy" in payload["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [float("nan"), float("inf"), -1.0, 0.0])
async def test_nonfinite_gather_budget_refused_loudly(
    beaver_db: AsyncBeaverDB, budget: float
) -> None:
    """Slice 8 (NaN/inf note): NaN slips through `<= 0` and inf is a
    pseudo-unbounded substrate wait — both refused at construction."""
    with pytest.raises(ValueError, match="gather_budget"):
        CompositeAgent(
            agent_id="comp",
            db=beaver_db,
            branches=[[("b1", "b1")]],
            gather_budget=budget,
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_nonfinite_lifecycle_budgets_rejected(value: float) -> None:
    """Slice 8 (NaN/inf note): drain budgets must be finite as well as > 0."""
    with pytest.raises(ValidationError):
        LifecycleParams(drain_timeout=value)
    with pytest.raises(ValidationError):
        LifecycleParams(drain_interval=value)


def test_yaml_cache_collection_names_broken_file(tmp_path) -> None:
    """Slice 8 (m8): a syntactically broken pattern file fails cache
    collection with a ConfigError naming the file (never a raw YAMLError)."""
    tool_dir = tmp_path / "tool"
    tool_dir.mkdir()
    (tool_dir / "broken.yaml").write_text("name: [unclosed\n", encoding="utf-8")
    ling_dir = tmp_path / "linguistic"
    ling_dir.mkdir()
    comp_dir = tmp_path / "composite"
    comp_dir.mkdir()
    loaded = LoadedConfig(
        config=LegioConfig(
            patterns=PatternsConfig(
                tool=tool_dir, linguistic=ling_dir, composite=comp_dir
            )
        ),
        secrets=EnvSecrets(),
        config_path=None,
    )
    with pytest.raises(ConfigError) as excinfo:
        _collect_spec_yamls(loaded)
    assert "broken.yaml" in str(excinfo.value)


@pytest.mark.asyncio
async def test_failed_drain_kick_submit_clears_coalesce_flag(
    beaver_db: AsyncBeaverDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 8 (coalesce ordering): a failed kick submit must not leave the
    coalesce flag set — the error is loud and the next kick still works."""
    runtime = Runtime(beaver_db, node_id="slice8@host")

    async def failing_submit(*args, **kwargs):
        raise RuntimeError("beaver down")

    monkeypatch.setattr(runtime.manager, "submit_task", failing_submit)
    with pytest.raises(RuntimeError, match="beaver down"):
        await runtime._kick_result_drain("unlucky_agent")
    assert "unlucky_agent" not in runtime._drain_inflight

    monkeypatch.undo()
    await runtime._kick_result_drain("unlucky_agent")
    assert await runtime.manager._pending.count() == 1


def build_race_composite(*, db: AsyncBeaverDB) -> GatherComposite:
    return GatherComposite(
        agent_id="comp",
        db=db,
        branches=[[("b1", "b1")], [("b2", "b2")]],
        input_as="main",
        output_as="comp",
    )


def race_request(task_id: str):
    return composite_request(
        task_id=task_id,
        payload={"main": {"seed": 1}},
        route=(("main", "main"), ("comp", "comp"), ("after", "after")),
        current_index=1,
        end_of_level_queue="result:" + task_id,
    )


@pytest.mark.asyncio
async def test_sequential_double_fan_out_stays_a_noop(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Guard: redelivering the same fan-out sequentially never duplicates
    slots or overwrites the winner's continuation."""
    comp = build_race_composite(db=beaver_db)
    await comp._fan_out(race_request("T-dup"))
    first = await comp._state.fetch("T-dup")
    await comp._fan_out(race_request("T-dup"))
    assert await comp._state.fetch("T-dup") == first
    assert await comp._slots.count() == 2


@pytest.mark.asyncio
async def test_lost_fan_out_race_cleans_only_own_slots(
    beaver_db: AsyncBeaverDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 8 (fan-out race): a worker that loses the fan-out race deletes
    only its own slots and leaves the winner's continuation intact."""
    comp = build_race_composite(db=beaver_db)
    winner = {
        "continuation": {"task_id": "T-race"},
        "expected": ["winner-branch"],
    }
    real_fetch = comp._state.fetch
    real_set = comp._state.set
    calls = {"count": 0}

    async def flaky_fetch(key: str):
        calls["count"] += 1
        if calls["count"] == 1:
            return None
        # A rival worker wins the race while this one is depositing.
        await real_set("T-race", dict(winner))
        return dict(winner)

    monkeypatch.setattr(comp._state, "fetch", flaky_fetch)
    await comp._fan_out(race_request("T-race"))

    assert await real_fetch("T-race") == winner
    assert await comp._slots.count() == 0
