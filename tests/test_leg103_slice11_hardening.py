"""Contract tests for LEG-103 Slice 11 — fifth-audit hardening.

Middleware predicate, yaml/config parity, tool/taxonomy gaps, bool
residuals, leg order uniformity, and clear logging.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from beaver import AsyncBeaverDB

from legio.config import _read_yaml
from legio.errors import ConfigError, LegioError, UnrecoverableError
from legio.federation import InterfaceMismatchError, UnresolvableAgentError
from legio.flow import SCHEMA_VERSION
from legio.naming import queue_key
from legio.runtime import Runtime
from legio.security import ClientTokenStore
from legio.security.middleware import AuthMiddleware, AuthorizationResult
from tests.test_leg022_toolagent import _run_tool_case, _tool_agent
from tests.test_leg085_runtime import _atomic_yaml, _load_atomic_spec
from tests.test_leg092_work_item import _client, build_app, capacity_catalog, work_item
from tests.test_leg095_db_proxy import bearer

NOT_A_TOOL = 42

_FEDERATION_LIST = [
    "POST /work-items/{agent}",
    "GET /work-items/{id}",
    "GET /outbox",
    "POST /outbox/{id}/ack",
    "GET /catalog",
    "GET /health",
    "POST /deposits",
]


def _middleware() -> AuthMiddleware:
    return AuthMiddleware(
        federation=("fed-secret", {"peer-a": "http://peer-a"}),
        clients=ClientTokenStore(),
    )


def test_middleware_method_prefixed_non_federation_path_is_not_federation() -> None:
    """Slice 11 (M1): a `METHOD /path` string is federation only for the
    explicit ARCH §10 path set — never for any method prefix."""
    middleware = _middleware()
    assert (
        middleware.authorize("POST /submit", token="fed-secret")
        == AuthorizationResult.UNAUTHORIZED_401
    )
    assert (
        middleware.authorize("DELETE /work-items", token="fed-secret")
        == AuthorizationResult.UNAUTHORIZED_401
    )
    assert (
        middleware.authorize("GET /outboxx", token="fed-secret")
        == AuthorizationResult.UNAUTHORIZED_401
    )


def test_middleware_federation_path_set_still_allowed() -> None:
    """Guard: the fixed predicate keeps the whole LEG-017 federation list."""
    middleware = _middleware()
    for endpoint in _FEDERATION_LIST:
        assert middleware.authorize(endpoint, token="fed-secret") in (
            AuthorizationResult.ALLOWED,
            AuthorizationResult.ALLOWED_FEDERATION,
        )


def test_bad_bytes_config_fails_as_config_error(tmp_path) -> None:
    """Slice 11 (M2): a bad-bytes config fails loud as ConfigError (Slice 9
    F11 parity with the CLI collector), never as a builtin traceback."""
    bad = tmp_path / "bad.yaml"
    bad.write_bytes(b"\xff\xfe\x00not-utf8")
    with pytest.raises(ConfigError):
        _read_yaml(bad)


def test_valid_config_still_reads(tmp_path) -> None:
    """Guard: genuine configs are untouched."""
    good = tmp_path / "good.yaml"
    good.write_text("node: {id: n1}\n", encoding="utf-8")
    assert _read_yaml(good) == {"node": {"id": "n1"}}


@pytest.mark.asyncio
async def test_non_callable_tool_is_unrecoverable(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Slice 11 (M3): a resolved-but-non-callable tool surfaces as
    UnrecoverableError (fatal authoring shape), never a bare TypeError."""
    agent, request = _tool_agent(
        beaver_db,
        task_id="T-noncallable",
        tool_name="shapeless",
        implementation="tests.test_leg103_slice11_hardening.NOT_A_TOOL",
        policy={"timeout": 30, "retries": 0},
    )
    payload = await _run_tool_case(beaver_db, agent, request)
    assert "UnrecoverableError" in payload["error"]
    assert "TypeError" not in payload["error"]


def test_federation_errors_are_unrecoverable() -> None:
    """Slice 11 (M4): the federation errors join the taxonomy — fatal
    authoring/config, still LegioErrors."""
    assert issubclass(InterfaceMismatchError, LegioError)
    assert issubclass(InterfaceMismatchError, UnrecoverableError)
    assert issubclass(UnresolvableAgentError, LegioError)
    assert issubclass(UnresolvableAgentError, UnrecoverableError)


@pytest.mark.asyncio
@pytest.mark.parametrize("pool", [True, False])
async def test_create_class_bool_pool_is_loud(beaver_db: AsyncBeaverDB, pool: bool) -> None:
    """Slice 11 (M5): a bool pool is a loud verb error — `True` never passes
    as a live instance, `False` never as disabled. The pump runs so the
    red path (silent acceptance + bring-up) fails fast instead of waiting
    out the lifecycle budget."""

    async def _pump(runtime: Runtime) -> None:
        while True:
            await runtime.manager.run()
            await asyncio.sleep(0)

    runtime = Runtime(beaver_db, node_id="slice11@host")
    pump = asyncio.create_task(_pump(runtime))
    name, spec = _load_atomic_spec("boolpool")
    try:
        with pytest.raises(ValueError, match="pool"):
            await runtime.create_class(spec, spec_yaml=_atomic_yaml("boolpool"), pool=pool)  # type: ignore[arg-type]
    finally:
        if await runtime.class_state(name) is not None:
            await runtime.destroy_class(name, mode="now")
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)


@pytest.mark.asyncio
async def test_create_instance_bool_count_is_loud(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Slice 11 (M5): a bool count is a loud verb error, never one instance
    (pump runs so the red path fails fast — see above)."""

    async def _pump(runtime: Runtime) -> None:
        while True:
            await runtime.manager.run()
            await asyncio.sleep(0)

    runtime = Runtime(beaver_db, node_id="slice11@host")
    pump = asyncio.create_task(_pump(runtime))
    name, spec = _load_atomic_spec("boolcount")
    await runtime.create_class(spec, spec_yaml=_atomic_yaml("boolcount"), pool=0)
    try:
        with pytest.raises(ValueError, match="count"):
            await runtime.create_instance(name, count=True)  # type: ignore[arg-type]
    finally:
        await runtime.destroy_class(name, mode="now")
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)


@pytest.mark.asyncio
async def test_direct_registry_bool_retries_fails_loudly(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Slice 11 (M6): `retries: False` on the direct-registry seam names the
    policy instead of passing silently as 0."""
    agent, request = _tool_agent(
        beaver_db,
        task_id="T-boolretries",
        tool_name="transform",
        implementation="tests.test_tools.fake_transform",
        policy={"timeout": 30, "retries": False},
    )
    payload = await _run_tool_case(beaver_db, agent, request)
    assert "boolean" in payload.get("error", "")


@pytest.mark.asyncio
async def test_work_item_stale_unknown_agent_is_409(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Slice 11 (M7): version-before-served on both legs — a stale-version
    work item for an unknown agent is 409 interface_mismatch (uniform with
    deposits), never a misleading 404."""
    app = build_app(beaver_db, capacity_catalog())
    async with await _client(app) as ac:
        resp = await ac.post(
            "/work-items/ghost",
            json=work_item(schema_version=SCHEMA_VERSION + 1),
            headers=bearer("fed-secret"),
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "interface_mismatch"


@pytest.mark.asyncio
async def test_destroy_with_queued_items_logs_inbox_clear(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture
) -> None:
    """Slice 11 (M8): destroying a class with queued items logs the inbox
    clear with the class and count (rule 11 parity with the result queue)."""
    caplog.set_level(logging.INFO, logger="legio.runtime")
    runtime = Runtime(beaver_db, node_id="slice11@host")
    name, spec = _load_atomic_spec("loudclear")
    await runtime.create_class(spec, spec_yaml=_atomic_yaml(name), pool=0)
    await beaver_db.queue(queue_key(name)).put({"probe": 1}, priority=0.0)
    await beaver_db.queue(queue_key(name)).put({"probe": 2}, priority=0.0)
    await runtime.destroy_class(name, mode="now")
    assert "inbox cleared" in caplog.text
    assert name in caplog.text
