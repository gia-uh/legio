"""Contract tests for LEG-103 Slice 12 — sixth-audit hardening.

Uniform pattern policy (all types), seed-envelope reads, loader YAMLError
wrap, plus token/config/CLI/log/monitor hardening (m1–m10). M2 is
withdrawn (counting proof); M4(a)/(b) stay withdrawn.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
import yaml
from beaver import AsyncBeaverDB
from pydantic import BaseModel, ValidationError

from legio.agents.base import AgentBase
from legio.agents.linguistic_agent import LinguisticAgent
from legio.api import DepositRequest, WorkItemRequest
from legio.cli import _as_int
from legio.config import LifecycleParams, ToolPolicy
from legio.errors import InvalidNameError, RecoverableError, UnrecoverableError
from legio.federation import Local, NodeDB, StepResolver
from legio.flow import ExecutionResultMessage
from legio.manager import Manager
from legio.materializer import _warn_unbounded_patterns
from legio.naming import queue_key, validate_agent_id
from legio.patterns import load_pattern_dirs, load_patterns
from legio.runtime import (
    _PENDING_CONTROLS_CAP,
    BRING_UP_TASK,
    Runtime,
)
from legio.security import ClientTokenStore
from tests.test_leg022_toolagent import _run_tool_case, _tool_agent, crafted_request
from tests.test_leg085_runtime import _atomic_yaml, _load_atomic_spec
from tests.test_leg091_step_resolver import capacity_catalog as _resolver_catalog
from tests.test_leg092_work_item import (
    _client,
    build_app,
    capacity_catalog,
)
from tests.test_leg095_db_proxy import (
    NODE_A,
    TOKEN,
    bearer,
)

NOT_A_TOOL = 42


class _Echo(BaseModel):
    text: str


class _HangingLingo:
    async def create(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        await asyncio.sleep(3600)


async def _slow_tool(text: str = "x") -> dict:
    await asyncio.sleep(0.5)
    return {"ok": True}


async def _pump(runtime: Runtime) -> None:
    while True:
        await runtime.manager.run()
        await asyncio.sleep(0)


def _atom_dict(name: str) -> dict:
    return yaml.safe_load(_atomic_yaml(name))


# --------------------------------------------------------------------------
# M1 — uniform pattern policy
# --------------------------------------------------------------------------


@pytest.mark.parametrize("timeout", [True, "5", 0, -1, float("inf")])
def test_pattern_policy_timeout_rejects_non_numbers(timeout: object) -> None:
    """Slice 12 (M1): a non-genuine policy timeout fails the whole load
    loudly — never silently dropped."""
    atom = _atom_dict("polbad")
    atom["policy"] = {"timeout": timeout}
    with pytest.raises(UnrecoverableError):
        load_patterns([atom])


def test_pattern_policy_forbids_tool_policy_fields() -> None:
    """Slice 12 (M1): agent policy and tool policy are different things —
    a `retries` copy-paste fails the load fast."""
    atom = _atom_dict("polmix")
    atom["policy"] = {"timeout": 5, "retries": 0}
    with pytest.raises(UnrecoverableError):
        load_patterns([atom])


def test_pattern_policy_timeout_loads() -> None:
    """Guard: a genuine timeout lands on the spec."""
    atom = _atom_dict("polok")
    atom["policy"] = {"timeout": 5}
    catalog = load_patterns([atom])
    assert catalog.specs["polok"].policy is not None
    assert catalog.specs["polok"].policy.timeout == 5


@pytest.mark.asyncio
async def test_linguistic_step_timeout_is_visible(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Slice 12 (M1): a hung LLM call dies by the step bound as a visible
    `TimeoutError` — the pump never parks silently."""
    agent = LinguisticAgent(
        agent_id="ling",
        db=beaver_db,
        lingo_client=_HangingLingo(),
        prompt_template="do it",
        output_model=_Echo,
        execution_timeout=0.05,
    )
    request = crafted_request(task_id="T-lingtimeout", payload={})
    await asyncio.wait_for(agent._run_guarded(request), 10)
    item = await beaver_db.queue(queue_key("main_a")).get(block=False)
    payload = dict(ExecutionResultMessage.model_validate(item.data).payload)
    assert "TimeoutError" in payload["error"]


@pytest.mark.asyncio
async def test_tool_pattern_timeout_bounds_whole_step(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Slice 12 (M1): the pattern bound caps the whole step even when the
    tool's own policy would allow longer."""
    agent, request = _tool_agent(
        beaver_db,
        task_id="T-steptimeout",
        tool_name="slowish",
        implementation="tests.test_leg103_slice12_hardening._slow_tool",
        policy={"timeout": 30, "retries": 0},
    )
    agent._execution_timeout = 0.05
    payload = await _run_tool_case(beaver_db, agent, request)
    assert "TimeoutError" in payload.get("error", "")


def test_absent_policy_loads_as_none() -> None:
    """Guard: patterns without policy keep today's unbounded behavior."""
    catalog = load_patterns([_atom_dict("nounbounded")])
    assert catalog.specs["nounbounded"].policy is None


def test_warn_unbounded_patterns_names_them(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Slice 12 (M1): the boot warning names each unbounded pattern."""
    catalog = load_patterns([_atom_dict("loudwarn")])
    with caplog.at_level(logging.WARNING, logger="legio.materializer"):
        _warn_unbounded_patterns(catalog)
    assert "loudwarn" in caplog.text
    assert "unbounded" in caplog.text


def test_agent_base_default_timeout_is_none(beaver_db: AsyncBeaverDB) -> None:
    """Guard: agents without policy keep today's unbounded behavior."""
    agent, _ = _tool_agent(
        beaver_db,
        task_id="T-notimeout",
        tool_name="transform",
        implementation="tests.test_tools.fake_transform",
        policy={"timeout": 30, "retries": 0},
    )
    assert isinstance(agent, AgentBase)
    assert agent._execution_timeout is None


# --------------------------------------------------------------------------
# M3 — seed-envelope reads
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_outbox_non_seed_reads_empty(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Slice 12 (M3): an existing non-seed task id reads empty — never a
    `KeyError` 500."""
    runtime = Runtime(beaver_db, node_id="slice12@host")
    pump = asyncio.create_task(_pump(runtime))
    name, spec = _load_atomic_spec("seedless")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("seedless"), pool=1)
        bring_up_id = next(iter(runtime._instance_tasks.values()))
        assert await runtime.read_outbox(bring_up_id) is None
    finally:
        await runtime.destroy_class(name, mode="now")
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)


@pytest.mark.asyncio
async def test_status_non_seed_is_unknown(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Slice 12 (M3): status on an internal id is stable `unknown task` —
    never a raw shape crash."""
    runtime = Runtime(beaver_db, node_id="slice12@host")
    pump = asyncio.create_task(_pump(runtime))
    name, spec = _load_atomic_spec("seedless2")
    try:
        await runtime.create_class(spec, spec_yaml=_atomic_yaml("seedless2"), pool=1)
        bring_up_id = next(iter(runtime._instance_tasks.values()))
        with pytest.raises(KeyError, match="unknown task"):
            await runtime.status(bring_up_id, None)
    finally:
        await runtime.destroy_class(name, mode="now")
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)


def test_bring_up_task_name_constant() -> None:
    """Guard: the envelope branch keys on the Runtime's own constant."""
    assert BRING_UP_TASK == "bring_up"


# --------------------------------------------------------------------------
# M4(c) — loader YAMLError wrap
# --------------------------------------------------------------------------


def test_bad_yaml_text_fails_unrecoverable() -> None:
    """Slice 12 (M4c): bad YAML text fails as `UnrecoverableError`
    (the loader's documented contract), never raw `YAMLError`."""
    with pytest.raises(UnrecoverableError) as excinfo:
        load_patterns("{{{\n: bad: [\n")
    assert "inline" in str(excinfo.value)


def test_bad_yaml_file_names_file(tmp_path) -> None:
    """Slice 12 (M4c): a bad file fails loudly naming the file."""
    tool_dir = tmp_path / "tool"
    tool_dir.mkdir()
    for dirname in ("linguistic", "composite"):
        (tmp_path / dirname).mkdir()
    (tool_dir / "broken.yaml").write_text("{{{\n: bad: [\n", encoding="utf-8")
    with pytest.raises(UnrecoverableError) as excinfo:
        load_pattern_dirs(
            {"tool": tool_dir, "linguistic": tmp_path / "linguistic", "composite": tmp_path / "composite"}
        )
    assert "broken.yaml" in str(excinfo.value)


# --------------------------------------------------------------------------
# m1 — deposit priority strictness
# --------------------------------------------------------------------------


@pytest.mark.parametrize("priority", [True, float("nan"), float("inf")])
def test_deposit_priority_rejects_bool_and_nonfinite(priority: object) -> None:
    """Slice 12 (m1): priority is a genuine finite number — bools and
    NaN/inf never reach beaver ordering."""
    with pytest.raises(ValidationError):
        DepositRequest(queue=queue_key("a"), item={}, priority=priority)  # type: ignore[arg-type]


def test_deposit_priority_genuine_passes() -> None:
    """Guard: real priorities are untouched."""
    assert DepositRequest(queue=queue_key("a"), item={}).priority == 0.0
    assert DepositRequest(queue=queue_key("a"), item={}, priority=2).priority == 2.0


# --------------------------------------------------------------------------
# m2 — string/bool numeric coercion residuals
# --------------------------------------------------------------------------


def test_budgets_reject_strings() -> None:
    """Slice 12 (m2): budgets are genuine numbers, never strings."""
    with pytest.raises(ValidationError):
        LifecycleParams(drain_timeout="30")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ToolPolicy(timeout="5")  # type: ignore[arg-type]


def test_schema_version_rejects_bool_and_str() -> None:
    """Slice 12 (m2): wire versions are genuine ints on both legs."""
    with pytest.raises(ValidationError):
        WorkItemRequest(
            task_id="n:11111111-1111-1111-1111-111111111111", payload={}, schema_version=True
        )
    with pytest.raises(ValidationError):
        DepositRequest(queue=queue_key("a"), item={}, schema_version="3")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# m3 — CLI _as_int strictness
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["3", True, 3.0])
def test_as_int_rejects_non_ints(value: object) -> None:
    """Slice 12 (m3): the programmatic seam takes genuine ints only —
    `"3"` never becomes 3 silently."""
    with pytest.raises(ValueError):
        _as_int(value, 1)


def test_as_int_genuine_and_default_pass() -> None:
    """Guard: real ints and None→default are untouched."""
    assert _as_int(3, 1) == 3
    assert _as_int(None, 1) == 1


# --------------------------------------------------------------------------
# m4 — constant-time token compares (guard-only: black-box equivalent)
# --------------------------------------------------------------------------


def test_token_compare_handles_all_inputs() -> None:
    """Slice 12 (m4): accept/reject semantics hold for every input shape,
    including unicode and empty (the hardening must change nothing
    observable)."""
    from legio.security import FederationTokenStore

    clients = ClientTokenStore()
    clients.register("a", token="sëcret-✓")
    assert clients.is_valid("a", "sëcret-✓") is True
    assert clients.is_valid("a", "wrong") is False
    assert clients.is_valid("ghost", "sëcret-✓") is False
    assert clients.resolve_consumer_id("sëcret-✓") == "a"
    assert clients.resolve_consumer_id("") is None
    store = FederationTokenStore(shared_token="fed")
    assert store.is_valid("fed") is True
    assert store.is_valid("") is False


# --------------------------------------------------------------------------
# m5 — token registry hygiene
# --------------------------------------------------------------------------


def test_duplicate_token_secret_refused() -> None:
    """Slice 12 (m5): the same secret for two consumers is refused loudly,
    naming the holder — ownership never ambiguous."""
    clients = ClientTokenStore()
    clients.register("a", token="shared-secret")
    with pytest.raises(ValueError, match="a"):
        clients.register("b", token="shared-secret")
    clients.register("a", token="shared-secret")


def test_revoke_absent_is_distinct_noop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Slice 12 (m5): revoking an absent id logs a noop, never a false
    'revoked'."""
    clients = ClientTokenStore()
    with caplog.at_level(logging.INFO, logger="legio.security"):
        clients.revoke("ghost")
    assert "noop" in caplog.text
    assert "revoked consumer=ghost" not in caplog.text


# --------------------------------------------------------------------------
# m6 — key=value log lines
# --------------------------------------------------------------------------


def test_naming_guard_logs_key_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Slice 12 (m6): the naming denial carries `name=`."""
    with pytest.raises(InvalidNameError):
        validate_agent_id("Bad Name!")
    assert "name=" in caplog.text


@pytest.mark.asyncio
async def test_catalog_denials_log_key_value(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture
) -> None:
    """Slice 12 (m6): catalog `unauthorized`/`no_capacity` carry keys."""
    app = build_app(beaver_db, None)
    async with await _client(app) as ac:
        with caplog.at_level(logging.WARNING):
            denied = await ac.get("/catalog")
            assert denied.status_code == 401
            assert "has_token=" in caplog.text
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            empty = await ac.get("/catalog", headers=bearer(TOKEN))
            assert empty.status_code == 503
            assert "configured=" in caplog.text


# --------------------------------------------------------------------------
# m7 — log the unlogged raise points
# --------------------------------------------------------------------------


def test_resolve_local_hit_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Slice 12 (m7): a local resolve decision is observable like
    remote/miss."""
    resolver = StepResolver(
        local_capacity=_resolver_catalog().served(), peer_catalogs={}
    )
    with caplog.at_level(logging.INFO, logger="legio.federation"):
        resolved = resolver.resolve("cutter")
    assert isinstance(resolved, Local)
    assert "cutter" in caplog.text


def test_non_callable_raise_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Slice 12 (m7): the non-callable refusal logs like the load
    failure does."""
    from legio.tools import AvailableToolsRegistry

    registry = AvailableToolsRegistry()
    registry.declare(
        "shapeless2", implementation="tests.test_leg103_slice12_hardening.NOT_A_TOOL"
    )
    with caplog.at_level(logging.ERROR, logger="legio.tools"), pytest.raises(
        Exception, match="non-callable"
    ):
        registry.load_tool("shapeless2")
    assert "shapeless2" in caplog.text


@pytest.mark.asyncio
async def test_verb_shape_reject_logs(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture
) -> None:
    """Slice 12 (m7): verb-shape rejects log like the unknown-class /
    instance denies do."""
    runtime = Runtime(beaver_db, node_id="slice12@host")
    with caplog.at_level(logging.WARNING, logger="legio.runtime"), pytest.raises(RecoverableError):
        await runtime.deposit_node_op("levitate", "ghostclass")
    assert "verb=" in caplog.text


@pytest.mark.asyncio
async def test_unknown_origin_logs(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture
) -> None:
    """Slice 12 (m7): the unknown result-origin refusal logs."""
    import httpx

    async with httpx.AsyncClient() as client:
        db = NodeDB(
            beaver_db,
            node_id=NODE_A,
            routes={},
            peers={},
            federation_token=TOKEN,
            client=client,
        )
        with caplog.at_level(logging.WARNING, logger="legio.federation"), pytest.raises(
            Exception, match="unknown author origin"
        ):
            db._queue_owner(
                queue_key("result:ghost:11111111-1111-1111-1111-111111111111")
            )
    assert "ghost" in caplog.text


# --------------------------------------------------------------------------
# m8 — pending-controls ledger cap
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_controls_cap_evicts_oldest_loudly(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture
) -> None:
    """Slice 12 (m8): the mint ledger is bounded — over cap evicts
    oldest-first with a visible warning (safe degradation via the
    existing orphan path)."""
    runtime = Runtime(beaver_db, node_id="slice12@host")
    for index in range(_PENDING_CONTROLS_CAP):
        runtime._pending_controls[(f"i-{index}", "enable", index)] = "c"
    with caplog.at_level(logging.WARNING, logger="legio.runtime"):
        await runtime._instance_control_fact("anyclass", "mint-0", "enable")
    assert len(runtime._pending_controls) == _PENDING_CONTROLS_CAP
    assert ("i-0", "enable", 0) not in runtime._pending_controls
    assert ("mint-0", "enable", 1) in runtime._pending_controls
    assert "evicted" in caplog.text


# --------------------------------------------------------------------------
# m9 — result shape, never values, in manager logs
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manager_success_logs_shape_not_values(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture
) -> None:
    """Slice 12 (m9): success events carry the result shape — domain
    values never reach the logs (rules 7/11)."""

    async def _echo() -> dict:
        return {"secret": "TOP-SECRET-VALUE"}

    with caplog.at_level(logging.INFO, logger="legio.manager"):
        manager = Manager(beaver_db, node_id="slice12@host")
        manager.register("echo", _echo)
        await manager.submit_task("echo")
        assert await manager.run() == 1
    assert "TOP-SECRET-VALUE" not in caplog.text
    assert "result=dict" in caplog.text


# --------------------------------------------------------------------------
# m10 — GET /health mounted
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_needs_federation_token(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Slice 12 (m10): /health is a guarded federation endpoint."""
    app = build_app(beaver_db, capacity_catalog())
    async with await _client(app) as ac:
        assert (await ac.get("/health")).status_code == 401
        assert (await ac.get("/health", headers=bearer("wrong"))).status_code == 401


@pytest.mark.asyncio
async def test_health_ok_without_catalog(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Slice 12 (m10): health answers especially with nothing configured."""
    app = build_app(beaver_db, None)
    async with await _client(app) as ac:
        resp = await ac.get("/health", headers=bearer(TOKEN))
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
