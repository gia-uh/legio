"""Contract tests for LEG-103 Slice 14 — eighth-audit hardening.

Destroy-mode envelope (M1), pending rehydrate (M2), corrupt-shape guards
(M3), registry index hardening (M4), plus intake/ledger/log hygiene
(m1–m13). M2-session-118 stays withdrawn (counting proof).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import pytest
import yaml
from beaver import AsyncBeaverDB
from pydantic import ValidationError

from legio.agents.composite_agent import CompositeAgent
from legio.config import (
    ApiConfig,
    ClientConfig,
    EnvSecrets,
    LegioConfig,
    LoadedConfig,
)
from legio.errors import ConfigError, RecoverableError, UnrecoverableError
from legio.flow import FlowToken
from legio.manager import TaskRecord, TaskStatus
from legio.materializer import _build_client_store, _materialize_atom
from legio.naming import ActivityState, queue_key
from legio.patterns import load_patterns
from legio.runtime import NodeOp, Runtime
from legio.security import ClientTokenStore
from legio.security.middleware import AuthMiddleware, AuthorizationResult
from tests.test_leg022_toolagent import _run_tool_case, _tool_agent
from tests.test_leg085_runtime import (
    _atomic_yaml,
    _composite_yaml,
    _load_atomic_spec,
)


def _atom_dict(name: str) -> dict:
    return yaml.safe_load(_atomic_yaml(name))


async def _pump(runtime: Runtime) -> None:
    while True:
        await runtime.manager.run()
        await asyncio.sleep(0)


# --------------------------------------------------------------------------
# M1 — destroy-mode envelope
# --------------------------------------------------------------------------


def test_node_op_mode_defaults_and_carries() -> None:
    """Slice 14 (M1): mode defaults to drain and carries when given."""
    assert NodeOp(verb="destroy_class", class_name="c").mode == "drain"
    assert NodeOp(verb="destroy_class", class_name="c", mode="now").mode == "now"


def test_node_op_bogus_mode_fails_at_construction() -> None:
    """Slice 14 (M1): a bogus mode never becomes an intent."""
    with pytest.raises(ValidationError):
        NodeOp(verb="destroy_class", class_name="c", mode="bogus")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_deposit_bogus_mode_is_loud(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Slice 14 (M1): bogus mode fails loudly at deposit — never silent drain."""
    runtime = Runtime(beaver_db, node_id="slice14@host")
    with pytest.raises(RecoverableError, match="mode"):
        await runtime.deposit_node_op("destroy_class", "ghost", mode="bogus")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_apply_threads_mode(
    beaver_db: AsyncBeaverDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 14 (M1): the drain relays the deposited mode to destroy."""
    seen: dict = {}
    runtime = Runtime(beaver_db, node_id="slice14@host")

    async def _spy(name: str, **kwargs) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(runtime, "destroy_class", _spy)
    await runtime._apply_node_op(NodeOp(verb="destroy_class", class_name="ghost", mode="now"))
    assert seen.get("mode") == "now"


# --------------------------------------------------------------------------
# M2 — pending rehydrate
# --------------------------------------------------------------------------


def _composite(beaver_db: AsyncBeaverDB) -> CompositeAgent:
    return CompositeAgent(
        agent_id="rc",
        db=beaver_db,
        branches=[[("x", "x")]],
        input_as="rc",
        output_as="rc",
        control_verifier=None,
    )


@pytest.mark.asyncio
async def test_rehydrate_restores_counter(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Slice 14 (M2): persisted continuations rebuild the gate after restart."""
    composite = _composite(beaver_db)
    await composite._state.set("t1", {"continuation": {}})
    await composite._state.set("t2", {"continuation": {}})
    assert await composite._has_pending() is False
    await composite.rehydrate()
    assert composite._pending_count == 2
    assert await composite._has_pending() is True


@pytest.mark.asyncio
async def test_rehydrate_is_idempotent(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Slice 14 (M2): re-calling rehydrate re-reads (assignment, not +=)."""
    composite = _composite(beaver_db)
    await composite._state.set("t1", {"continuation": {}})
    await composite.rehydrate()
    await composite.rehydrate()
    assert composite._pending_count == 1


@pytest.mark.asyncio
async def test_fresh_composite_starts_at_zero(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Guard: no persisted work means a closed gate, as before."""
    composite = _composite(beaver_db)
    assert composite._pending_count == 0
    assert await composite._has_pending() is False


# --------------------------------------------------------------------------
# M3 — corrupt-shape guards
# --------------------------------------------------------------------------


def _seed_record(task_id: str, **kwargs: object) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        name="seed",
        status=TaskStatus.SUCCESS,
        enqueued_at=datetime.now(UTC).isoformat(),
        finished_at=datetime.now(UTC).isoformat(),
        kwargs=dict(kwargs),
    )


@pytest.mark.asyncio
async def test_read_outbox_corrupt_token_reads_empty(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture
) -> None:
    """Slice 14 (M3): a seed record with a malformed token reads empty —
    never a raw ValidationError."""
    runtime = Runtime(beaver_db, node_id="slice14@host")
    task_id = "slice14@host:badtoken"
    record = _seed_record(task_id, token="garbage", client_id="c")
    await runtime.manager._tasks.set(task_id, record.model_dump(mode="json"))
    with caplog.at_level(logging.WARNING, logger="legio.runtime"):
        assert await runtime.read_outbox(task_id) is None
    assert task_id in caplog.text


@pytest.mark.asyncio
async def test_status_corrupt_token_is_unknown(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture
) -> None:
    """Slice 14 (M3): status on a malformed-token seed is stable unknown."""
    runtime = Runtime(beaver_db, node_id="slice14@host")
    task_id = "slice14@host:badtoken2"
    record = _seed_record(task_id, token={"nope": 1}, client_id="c")
    await runtime.manager._tasks.set(task_id, record.model_dump(mode="json"))
    with (
        caplog.at_level(logging.WARNING, logger="legio.runtime"),
        pytest.raises(KeyError, match="unknown task"),
    ):
        await runtime.status(task_id, "c")
    assert task_id in caplog.text


@pytest.mark.asyncio
async def test_read_outbox_corrupt_bytes_read_empty(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture
) -> None:
    """Slice 14 (M3): corrupt outbox bytes read empty with a warning."""
    runtime = Runtime(beaver_db, node_id="slice14@host")
    task_id = "slice14@host:badbytes"
    token = FlowToken().model_dump(mode="json")
    record = _seed_record(task_id, token=token, client_id="c")
    await runtime.manager._tasks.set(task_id, record.model_dump(mode="json"))
    await runtime._outbox.set(task_id, {"garbage": True})
    with caplog.at_level(logging.WARNING, logger="legio.runtime"):
        assert await runtime.read_outbox(task_id) is None
    assert task_id in caplog.text


# --------------------------------------------------------------------------
# M4 — registry index hardening
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_class_drives_from_index(
    beaver_db: AsyncBeaverDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 14 (M4): remove_class never iterates the instances scope —
    it drives from the index."""
    from legio.registry import Registry

    registry = Registry(beaver_db)
    name, spec = _load_atomic_spec("indexed")
    await registry.record_class(
        name,
        spec.kind,
        dependencies=[],
        queue=queue_key(name),
        state=ActivityState.ENABLED,
    )
    await registry.record_instance(name, "i-0", state=ActivityState.ENABLED)
    other, ospec = _load_atomic_spec("bystander")
    await registry.record_class(
        other,
        ospec.kind,
        dependencies=[],
        queue=queue_key(other),
        state=ActivityState.ENABLED,
    )
    await registry.record_instance(other, "i-9", state=ActivityState.ENABLED)

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
    await registry.remove_class(name)
    assert iters == 0
    assert await registry._instances.fetch(f"{name}:i-0") is None
    assert await registry._instances.fetch(f"{other}:i-9") is not None
    assert await registry._instances_by_class.fetch(name) is None


@pytest.mark.asyncio
async def test_index_miss_heals_with_warning(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture
) -> None:
    """Slice 14 (M4): stored-enabled + empty index re-verifies by scan
    (crash-divergence backstop) instead of misreading DISABLED."""
    from legio.registry import Registry

    registry = Registry(beaver_db)
    name, spec = _load_atomic_spec("diverged")
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
        assert await registry.class_state(name) == ActivityState.ENABLED
    assert "index miss" in caplog.text


# --------------------------------------------------------------------------
# m1 — loader read_text wrap
# --------------------------------------------------------------------------


def test_unreadable_pattern_file_names_file(tmp_path) -> None:
    """Slice 14 (m1): an unreadable pattern file fails naming the file."""
    from legio.patterns import load_pattern_dirs

    tool_dir = tmp_path / "tool"
    tool_dir.mkdir()
    for dirname in ("linguistic", "composite"):
        (tmp_path / dirname).mkdir()
    (tool_dir / "locked.yaml").mkdir()
    with pytest.raises(UnrecoverableError) as excinfo:
        load_pattern_dirs(
            {"tool": tool_dir, "linguistic": tmp_path / "linguistic", "composite": tmp_path / "composite"}
        )
    assert "locked.yaml" in str(excinfo.value)


def test_bad_bytes_pattern_file_is_unrecoverable(tmp_path) -> None:
    """Slice 14 (m1): non-UTF8 bytes fail as UnrecoverableError (parity)."""
    from legio.patterns import load_pattern_dirs

    tool_dir = tmp_path / "tool"
    tool_dir.mkdir()
    for dirname in ("linguistic", "composite"):
        (tmp_path / dirname).mkdir()
    (tool_dir / "binary.yaml").write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(UnrecoverableError):
        load_pattern_dirs(
            {"tool": tool_dir, "linguistic": tmp_path / "linguistic", "composite": tmp_path / "composite"}
        )


# --------------------------------------------------------------------------
# m2 — state-report poison parity
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_corrupt_report_warns_consumes_replenishes(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture
) -> None:
    """Slice 14 (m2): a corrupt report warns, is consumed, and the drain
    is rescheduled while work remains (result-drain parity)."""
    runtime = Runtime(beaver_db, node_id="slice14@host")
    await runtime._state_reports.put({"junk": 1}, priority=0.0)
    await runtime._state_reports.put({"junk": 2}, priority=0.0)
    with caplog.at_level(logging.WARNING, logger="legio.runtime"):
        result = await runtime._state_report_fact()
    assert result["processed"] == 1
    assert result["replenished"] is True
    assert "junk" in caplog.text or "invalid" in caplog.text


# --------------------------------------------------------------------------
# m3 — ledger/task-entry leaks plugged
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purge_pending_drops_seq(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Slice 14 (m3): purge drops control-sequence entries too."""
    runtime = Runtime(beaver_db, node_id="slice14@host")
    runtime._pending_controls[("i", "enable", 1)] = "c"
    runtime._control_sequence[("c", "i")] = 7
    runtime._purge_pending("i")
    assert ("i", "enable", 1) not in runtime._pending_controls
    assert ("c", "i") not in runtime._control_sequence


@pytest.mark.asyncio
async def test_bring_up_drops_task_entry_on_confirm_failure(
    beaver_db: AsyncBeaverDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 14 (m3): a failed bring-up confirm leaves no task entry."""
    from legio.errors import RecoverableError

    runtime = Runtime(beaver_db, node_id="slice14@host")

    async def _fail(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RecoverableError("confirm exploded")

    monkeypatch.setattr(runtime, "_await_observable_state", _fail)
    with pytest.raises(RecoverableError, match="confirm exploded"):
        await runtime._bring_up("zz", ActivityState.DISABLED)
    assert all(key[0] != "zz" for key in runtime._instance_tasks)


# --------------------------------------------------------------------------
# m4 — retries genuine-int on the direct path
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_registry_float_retries_fails_loudly(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Slice 14 (m4): `retries: 0.0` names the policy (file-path parity)."""
    agent, request = _tool_agent(
        beaver_db,
        task_id="T-floatretries",
        tool_name="transform",
        implementation="tests.test_tools.fake_transform",
        policy={"timeout": 30, "retries": 0.0},
    )
    payload = await _run_tool_case(beaver_db, agent, request)
    assert "retries" in payload.get("error", "")
    assert "integer" in payload.get("error", "")


# --------------------------------------------------------------------------
# m5 — loader rejections log
# --------------------------------------------------------------------------


def test_unknown_branch_step_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Slice 14 (m5): unknown branch steps log naming the pattern."""
    comp = yaml.safe_load(_composite_yaml("ghostcomp", ["ghost-step"]))
    with (
        caplog.at_level(logging.WARNING, logger="legio.patterns.loader"),
        pytest.raises(UnrecoverableError),
    ):
        load_patterns([_atom_dict("ghostcomp-atom"), comp])
    assert "ghostcomp" in caplog.text


def test_duplicate_pattern_name_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Slice 14 (m5): duplicate names log naming the pattern."""
    atom = _atom_dict("dupname")
    with (
        caplog.at_level(logging.WARNING, logger="legio.patterns.loader"),
        pytest.raises(UnrecoverableError),
    ):
        load_patterns([atom, dict(atom)])
    assert "dupname" in caplog.text


def test_non_mapping_document_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Slice 14 (m5): non-mapping documents log."""
    with (
        caplog.at_level(logging.WARNING, logger="legio.patterns.loader"),
        pytest.raises(UnrecoverableError),
    ):
        load_patterns(["just-a-string"])  # type: ignore[list-item]
    assert "mapping" in caplog.text


# --------------------------------------------------------------------------
# m6 — materializer refuses log
# --------------------------------------------------------------------------


def test_linguistic_without_output_schema_logs(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture
) -> None:
    """Slice 14 (m6): the linguistic no-schema refusal logs the agent."""
    ling = _atom_dict("linglog")
    ling["kind"] = "linguistic"
    ling.pop("tool", None)
    ling.pop("parameters", None)
    ling["prompt"] = "do {text}"
    spec = load_patterns([ling]).specs["linglog"]
    spec.output.output_schema = None  # bypass load-time shape; pin the materializer refusal

    with (
        caplog.at_level(logging.ERROR, logger="legio.materializer"),
        pytest.raises(UnrecoverableError),
    ):
        _materialize_atom(
            spec,
            db=beaver_db,
            available_tools=None,  # type: ignore[arg-type]
            lingo_client=None,
            control_verifier=None,
        )
    assert "linglog" in caplog.text


def test_unknown_kind_logs(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture
) -> None:
    """Slice 14 (m6): the unknown-kind refusal logs the agent."""
    spec = load_patterns([_atom_dict("kindlog")]).specs["kindlog"]
    spec.kind = "bogus"  # type: ignore[assignment]

    with (
        caplog.at_level(logging.ERROR, logger="legio.materializer"),
        pytest.raises(UnrecoverableError),
    ):
        _materialize_atom(
            spec,
            db=beaver_db,
            available_tools=None,  # type: ignore[arg-type]
            lingo_client=None,
            control_verifier=None,
        )
    assert "kindlog" in caplog.text


# --------------------------------------------------------------------------
# m7 — bounded-wait terminals log
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_failure_logs(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture
) -> None:
    """Slice 14 (m7): a failed confirm logs before raising."""
    runtime = Runtime(beaver_db, node_id="slice14@host")

    async def _boom() -> dict:
        raise RuntimeError("confirm-target exploded")

    manager = runtime.manager
    manager.register("boom7", _boom)
    task_id = await manager.submit_task("boom7")
    await manager.run()
    with (
        caplog.at_level(logging.WARNING, logger="legio.runtime"),
        pytest.raises(RecoverableError, match="confirm-target exploded"),
    ):
        await runtime._await_observable_state(
            task_id,
            lambda record: False,
            what="probe-confirm",
            class_name="anyclass",
        )
    assert "probe-confirm" in caplog.text


@pytest.mark.asyncio
async def test_report_failure_logs(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture
) -> None:
    """Slice 14 (m7): a failed bring-up record logs on the report path."""
    runtime = Runtime(beaver_db, node_id="slice14@host")

    async def _boom() -> dict:
        raise RuntimeError("report-target exploded")

    manager = runtime.manager
    manager.register("boom7r", _boom)
    task_id = await manager.submit_task("boom7r")
    await manager.run()
    await runtime.registry.record_class(
        "rc", None, dependencies=[], queue=queue_key("rc"), state=ActivityState.DISABLED
    )
    await runtime.registry.record_instance("rc", "i-7", state=ActivityState.DISABLED)
    runtime._instance_tasks[("rc", "i-7")] = task_id
    with (
        caplog.at_level(logging.WARNING, logger="legio.runtime"),
        pytest.raises(RecoverableError),
    ):
        await runtime._await_instance_state("rc", "i-7", ActivityState.ENABLED, what="probe-report")
    assert "probe-report" in caplog.text


@pytest.mark.asyncio
async def test_drain_timeout_logs(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 14 (m7): a drain timeout logs before raising."""
    runtime = Runtime(beaver_db, node_id="slice14@host")
    name, spec = _load_atomic_spec("draintimer")
    await runtime.create_class(spec, spec_yaml=_atomic_yaml("draintimer"), pool=0)
    try:
        await beaver_db.queue(queue_key(name)).put({"probe": 1}, priority=0.0)

        async def _tiny_budget(*args, **kwargs):  # type: ignore[no-untyped-def]
            return (0.02, 0.005)

        monkeypatch.setattr(runtime, "_lifecycle_budget", _tiny_budget)
        with (
            caplog.at_level(logging.WARNING, logger="legio.runtime"),
            pytest.raises(RecoverableError),
        ):
            await runtime._await_queue_empty(name, prior_gate=None)
        assert "timed out" in caplog.text
    finally:
        await runtime.destroy_class(name, mode="now")


# --------------------------------------------------------------------------
# m8 — destroy validates mode first
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_destroy_unknown_bogus_mode_is_loud(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Slice 14 (m8): unknown class + bogus mode is a loud ValueError
    (Slice 9 F5 order), never a silent noop."""
    runtime = Runtime(beaver_db, node_id="slice14@host")
    with pytest.raises(ValueError, match="mode"):
        await runtime.destroy_class("ghost-xyz", mode="bogus")  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# m9 — tool monitor event
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_failure_emits_step_error(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Slice 14 (m9): tool failures emit step_error to monitors
    (linguistic parity)."""
    from dataclasses import dataclass, field

    @dataclass
    class _Recorder:
        events: list = field(default_factory=list)

        async def monitor(self, agent_id: str, task_id: str, event: str) -> None:
            self.events.append((event, task_id))

    recorder = _Recorder()
    # drive a failing tool: unknown tool name fails at load inside _handle
    agent2, request2 = _tool_agent(
        beaver_db,
        task_id="T-monevent2",
        tool_name="ghost-tool",
        implementation="no.such.mod",
        policy={"timeout": 30, "retries": 0},
    )
    agent2.set_hooks(monitor=recorder.monitor)
    payload = await _run_tool_case(beaver_db, agent2, request2)
    assert "error" in payload
    assert ("step_error", "T-monevent2") in recorder.events


# --------------------------------------------------------------------------
# m11 — middleware secret compare parity
# --------------------------------------------------------------------------


def test_middleware_secret_compare_parity() -> None:
    """Slice 14 (m11): middleware verdicts match the stores on every
    input shape (guard-only: black-box equivalent)."""
    clients = ClientTokenStore()
    clients.register("c", token="sëcret-✓")
    middleware = AuthMiddleware(
        federation=("fed-secret", {"peer-a": "http://peer-a"}),
        clients=clients,
    )

    assert middleware.authorize("GET /catalog", token="fed-secret") in (
        AuthorizationResult.ALLOWED,
        AuthorizationResult.ALLOWED_FEDERATION,
    )
    assert middleware.authorize("GET /catalog", token="fëd-sëcret") == AuthorizationResult.UNAUTHORIZED_401
    assert middleware.authorize("GET /catalog", token="x" * 500) == AuthorizationResult.UNAUTHORIZED_401
    assert middleware.authorize("submit(starting_agent)", token="sëcret-✓") == AuthorizationResult.ALLOWED


# --------------------------------------------------------------------------
# m12 — duplicate boot secrets name the holder
# --------------------------------------------------------------------------


def test_duplicate_boot_secrets_name_holder() -> None:
    """Slice 14 (m12): duplicate client secrets fail boot as ConfigError
    naming the holder (fatal operator authoring)."""
    loaded = LoadedConfig(
        config=LegioConfig(api=ApiConfig(clients={"a": ClientConfig(), "b": ClientConfig()})),
        secrets=EnvSecrets(client_tokens={"a": "same", "b": "same"}),
        config_path=None,
    )
    with pytest.raises(ConfigError, match="already held by consumer"):
        _build_client_store(loaded)


# --------------------------------------------------------------------------
# m13 — None lingo refused naming the agent
# --------------------------------------------------------------------------


def test_none_lingo_refused_naming_agent(beaver_db: AsyncBeaverDB) -> None:
    """Slice 14 (m13): a None lingo result fails fast naming the agent."""
    ling = _atom_dict("lingnone")
    ling["kind"] = "linguistic"
    ling.pop("tool", None)
    ling.pop("parameters", None)
    ling["prompt"] = "do {text}"
    spec = load_patterns([ling]).specs["lingnone"]

    with pytest.raises(UnrecoverableError, match="lingnone"):
        _materialize_atom(
            spec,
            db=beaver_db,
            available_tools=None,  # type: ignore[arg-type]
            lingo_client=None,
            control_verifier=None,
        )
