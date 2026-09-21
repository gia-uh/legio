"""Contract tests for LEG-103 Slice 10 — design/coupling audit hardening.

Version-aware cross-node delegation, a guarded executor loop, strict
lifecycle verbs, error-taxonomy discipline, node-op intake hygiene,
duplicate cache names, destroy-crash posture, export completeness, and
genuine-int config ports.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
import respx
import typer
from beaver import AsyncBeaverDB
from pydantic import ValidationError

from legio.agents.base import ContractError
from legio.api import create_app
from legio.cli import _collect_spec_yamls, _run_cli
from legio.config import (
    ApiConfig,
    EmbeddingConfig,
    EnvSecrets,
    LegioConfig,
    LoadedConfig,
    PatternsConfig,
)
from legio.errors import ConfigError, LegioError, UnrecoverableError
from legio.federation import SCHEMA_VERSION
from legio.flow import SCHEMA_VERSION as FLOW_SCHEMA_VERSION
from legio.manager import Manager
from legio.naming import queue_key
from legio.registry import ActivityState
from legio.runtime import Runtime
from tests.test_leg022_toolagent import _run_tool_case, _tool_agent
from tests.test_leg085_runtime import _atomic_yaml, _load_atomic_spec
from tests.test_leg095_db_proxy import (
    NODE_A,
    NODE_B,
    PEER_B_URL,
    TOKEN,
    bearer,
    tool_catalog,
)

assert SCHEMA_VERSION == FLOW_SCHEMA_VERSION


def _deposit_app(beaver_db: AsyncBeaverDB, agent: str):
    runtime = Runtime(beaver_db, node_id=NODE_B)
    return create_app(runtime=runtime, pattern_catalog=tool_catalog(agent), federation_token=TOKEN)


async def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_deposits_version_mismatch_is_409(beaver_db: AsyncBeaverDB) -> None:
    """Slice 10 (F1): a stale-version cross-node deposit is refused loudly —
    never executed silently."""
    app = _deposit_app(beaver_db, "cutter")
    async with await _client(app) as ac:
        resp = await ac.post(
            "/deposits",
            json={
                "queue": queue_key("cutter"),
                "item": {"task_id": "node-a@test:tid"},
                "priority": 0.0,
                "schema_version": SCHEMA_VERSION + 1,
            },
            headers=bearer(TOKEN),
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "interface_mismatch"
        assert await beaver_db.queue(queue_key("cutter")).count() == 0


@pytest.mark.asyncio
async def test_deposits_current_and_defaulted_versions_land(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Slice 10 (F1): current-version and versionless (defaulted) deposits
    still land — the gate only bites on skew."""
    app = _deposit_app(beaver_db, "cutter")
    async with await _client(app) as ac:
        current = await ac.post(
            "/deposits",
            json={
                "queue": queue_key("cutter"),
                "item": {"task_id": "node-a@test:tid"},
                "priority": 0.0,
                "schema_version": SCHEMA_VERSION,
            },
            headers=bearer(TOKEN),
        )
        assert current.status_code == 200
        defaulted = await ac.post(
            "/deposits",
            json={"queue": queue_key("cutter"), "item": {"task_id": "node-a@test:tid"}},
            headers=bearer(TOKEN),
        )
        assert defaulted.status_code == 200
        assert await beaver_db.queue(queue_key("cutter")).count() == 2


@pytest.mark.asyncio
@respx.mock
async def test_remote_queue_stamps_schema_version(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Slice 10 (F1): every proxy `put` stamps the current version on the
    wire so the owner can gate it."""
    from legio.federation import NodeDB

    route = respx.post(f"{PEER_B_URL}/deposits").mock(
        return_value=httpx.Response(200, json={"queue": queue_key("cutter"), "deposited": True})
    )
    async with httpx.AsyncClient() as client:
        db = NodeDB(
            beaver_db,
            node_id=NODE_A,
            routes={"cutter": NODE_B},
            peers={NODE_B: PEER_B_URL},
            federation_token=TOKEN,
            client=client,
        )
        await db.queue(queue_key("cutter")).put({"task_id": "T-1"}, priority=0.0)
    assert route.called
    body = json.loads(route.calls[0].request.content.decode())
    assert body["schema_version"] == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_crashing_dispatch_logs_with_task_id_and_raises(
    beaver_db: AsyncBeaverDB,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slice 10 (F2): an ESCAPING dispatch error (machinery crash — a task
    failure is recorded FAILED by ``_await_plain``/``_drive_parked`` and never
    crashes the pump) logs the exception with its task id and still raises, so
    the pump fleet never shrinks silently."""

    async def _machinery_crash(task_id: str) -> None:
        raise RuntimeError("tasks store exploded")

    caplog.set_level(logging.ERROR, logger="legio.manager")
    manager = Manager(beaver_db, node_id="slice10@host")
    task_id = await manager.submit_task("noop")
    monkeypatch.setattr(manager, "_dispatch", _machinery_crash)
    with pytest.raises(RuntimeError, match="tasks store exploded"):
        await manager.run()
    assert task_id in caplog.text


@pytest.mark.asyncio
async def test_direct_registry_bool_timeout_fails_loudly(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Slice 10 (F3): `timeout: True` on the direct-registry seam names the
    policy instead of becoming a silent 1 s timeout."""
    agent, request = _tool_agent(
        beaver_db,
        task_id="T-booltimeout",
        tool_name="transform",
        implementation="tests.test_tools.fake_transform",
        policy={"timeout": True, "retries": 0},
    )
    payload = await _run_tool_case(beaver_db, agent, request)
    assert "boolean" in payload["error"]


@pytest.mark.asyncio
async def test_create_instance_zero_count_is_loud(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Slice 10 (F4): `count < 1` is a loud verb error, never a silent noop."""
    runtime = Runtime(beaver_db, node_id="slice10@host")
    name, spec = _load_atomic_spec("zerocount")
    await runtime.create_class(spec, spec_yaml=_atomic_yaml("zerocount"), pool=0)
    try:
        with pytest.raises(ValueError, match="count"):
            await runtime.create_instance(name, count=0)
    finally:
        await runtime.destroy_class(name, mode="now")


@pytest.mark.asyncio
async def test_create_class_negative_pool_is_loud(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Slice 10 (F4): `pool < 0` is a loud verb error, never a recorded
    disabled class."""
    runtime = Runtime(beaver_db, node_id="slice10@host")
    name, spec = _load_atomic_spec("negpool")
    with pytest.raises(ValueError, match="pool"):
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("negpool"), pool=-1)
    assert await runtime.class_state(name) is None


def test_contract_error_is_a_legio_error() -> None:
    """Slice 10 (F5): the taxonomy holds — every legio error is a LegioError."""
    assert issubclass(ContractError, LegioError)
    assert issubclass(ContractError, UnrecoverableError)


@pytest.mark.asyncio
async def test_tool_load_failure_is_typed(beaver_db: AsyncBeaverDB) -> None:
    """Slice 10 (F6): an unloadable tool surfaces as UnrecoverableError,
    never a bare RuntimeError."""
    agent, request = _tool_agent(
        beaver_db,
        task_id="T-badload",
        tool_name="ghost",
        implementation="no.such.module_here",
        policy={"timeout": 30, "retries": 0},
    )
    payload = await _run_tool_case(beaver_db, agent, request)
    assert "UnrecoverableError" in payload["error"]
    assert "RuntimeError" not in payload["error"]


def test_cli_maps_builtin_failures_to_loud_exits() -> None:
    """Slice 10 (F6): KeyError/ValueError at the CLI boundary become
    `legio error:` exits instead of tracebacks."""

    async def _key() -> int:
        raise KeyError("ghost-class")

    async def _value() -> int:
        raise ValueError("bad mode")

    for coro in (_key(), _value()):
        with pytest.raises(typer.Exit) as excinfo:
            _run_cli(coro)
        assert excinfo.value.exit_code == 1


@pytest.mark.asyncio
async def test_failed_node_op_submit_leaves_no_stranded_intent(
    beaver_db: AsyncBeaverDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 10 (F7): a failed drain-task submit removes the just-queued
    intent while the original error propagates loudly."""

    async def failing_submit(*args, **kwargs):
        raise RuntimeError("beaver down")

    runtime = Runtime(beaver_db, node_id="slice10@host")
    name, spec = _load_atomic_spec("opclass")
    await runtime.create_class(spec, spec_yaml=_atomic_yaml("opclass"), pool=0)
    try:
        monkeypatch.setattr(runtime.manager, "submit_task", failing_submit)
        with pytest.raises(RuntimeError, match="beaver down"):
            await runtime.deposit_node_op("enable_class", name)
        assert await beaver_db.queue("node_ops").count() == 0
    finally:
        await runtime.destroy_class(name, mode="now")


def _pattern_config(tmp_path, files: dict[str, str]) -> LoadedConfig:
    tool_dir = tmp_path / "tool"
    tool_dir.mkdir(exist_ok=True)
    for filename, body in files.items():
        (tool_dir / filename).write_text(body, encoding="utf-8")
    for dirname in ("linguistic", "composite"):
        (tmp_path / dirname).mkdir(exist_ok=True)
    return LoadedConfig(
        config=LegioConfig(
            patterns=PatternsConfig(
                tool=tool_dir,
                linguistic=tmp_path / "linguistic",
                composite=tmp_path / "composite",
            )
        ),
        secrets=EnvSecrets(),
        config_path=None,
    )


def test_yaml_cache_duplicate_names_fail_loudly(tmp_path) -> None:
    """Slice 10 (F8): a duplicate pattern name across cache files names both
    files (the loader already rejects duplicates loudly)."""
    loaded = _pattern_config(
        tmp_path,
        {
            "a.yaml": "name: dup\n",
            "b.yaml": "name: dup\n",
        },
    )
    with pytest.raises(ConfigError) as excinfo:
        _collect_spec_yamls(loaded)
    message = str(excinfo.value)
    assert "a.yaml" in message and "b.yaml" in message


@pytest.mark.asyncio
async def test_mid_destroy_crash_keeps_gate_closed(
    beaver_db: AsyncBeaverDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 10 (F9): keep-closed is the documented safe posture — a crash
    mid-destroy never re-admits work into a half-destroyed class."""

    async def failing_clear(name: str) -> None:
        raise RuntimeError("clear exploded")

    runtime = Runtime(beaver_db, node_id="slice10@host")
    name, spec = _load_atomic_spec("doomed")
    await runtime.create_class(spec, spec_yaml=_atomic_yaml("doomed"), pool=0)
    monkeypatch.setattr(runtime, "_clear_queue", failing_clear)
    with pytest.raises(RuntimeError, match="clear exploded"):
        await runtime.destroy_class(name, mode="now")
    gate = await runtime._gates.fetch(name)
    assert gate is not None and gate.get("state") == ActivityState.DISABLED.value


def test_materializer_and_config_exports_complete() -> None:
    """Slice 10 (F10): public aliases and constants ride `__all__`."""
    import legio.config as config_module
    import legio.materializer as materializer_module

    assert "LingoFactory" in materializer_module.__all__
    assert "CompositeClasses" in materializer_module.__all__
    assert "SECRET_ENV_NAMES" in config_module.__all__
    assert "CLIENT_TOKEN_PREFIX" in config_module.__all__


@pytest.mark.parametrize("port", [True, "8000", 8000.0])
def test_api_port_requires_genuine_int(port) -> None:
    """Slice 10 (F11): ports coerce silently today — genuine ints only."""
    with pytest.raises(ValidationError):
        ApiConfig(port=port)


def test_api_port_genuine_int_passes() -> None:
    """Guard: a real port is untouched."""
    assert ApiConfig(port=8000).port == 8000


@pytest.mark.parametrize("batch", [True, 1.5, "64"])
def test_embedding_batch_requires_genuine_int(batch) -> None:
    """Slice 10 (F11): batch sizes coerce silently today — genuine ints only."""
    with pytest.raises(ValidationError):
        EmbeddingConfig(base_url="http://x", max_tokens_per_batch=batch)


def test_embedding_batch_genuine_int_and_absent_pass() -> None:
    """Guard: real and absent batch sizes are untouched."""
    assert (
        EmbeddingConfig(base_url="http://x", max_tokens_per_batch=100).max_tokens_per_batch == 100
    )
    assert EmbeddingConfig(base_url="http://x").max_tokens_per_batch is None


def test_tool_catalog_helper_loads() -> None:
    """Guard: the shared deposits fixture still loads (borrowed helper)."""
    assert "cutter" in tool_catalog("cutter").specs
