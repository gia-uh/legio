"""Contract tests for LEG-081 — the Runtime CLI (typer).

``legio server`` boots the node and serves ``submit``/``status`` (the §8
bootstrap via ``bring_catalog_up``); ``legio agent`` maps 1:1 to the Runtime's
lifecycle verbs, each invocation booting the node in-process with a per-boot
control key (LEG-082) and driving its own executor. Rules under test:

- config loading / precedence / loud failures (``ConfigError``);
- executor ownership and pump sizing (a parked bring-up generator occupies a
  pump for the agent's whole life, so the fleet = served pools + spares);
- the server surface: catalog up (§8), submit/status over the app, SIGTERM
  drains in-flight work before exit (host stops driving between dispatches);
- agent verb mapping: create/recreate/instances/reads direct, enable/disable/
  destroy via the node-op intake, each reachable and erroring loudly (rule 9).

The validation case (tool-only node, §7) runs through the real CLI process:
a subprocess ``python -m legio server`` serves the ``transform`` agent and
drains a submitted item at ``SIGTERM``.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from beaver import AsyncBeaverDB

from legio.cli import (
    _shutdown_pumps,
    agent_command,
    bring_catalog_up,
    executor_loop,
    executor_pump_count,
)
from legio.config import CliOverrides, load
from legio.errors import ConfigError, LegioError
from legio.materializer import boot_node
from legio.naming import outbox_key

TRANSFORM_YAML = """\
name: transform
type: atomic
kind: tool
main: true
input:
  input_as: transform
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
      factor: {type: integer}
output:
  output_as: transform
  output_type: json
  output_schema:
    type: object
    properties:
      transformed: {type: string}
tool: transform
parameters:
  text: "{transform.text}"
  factor: "{transform.factor}"
"""

TOOLS_YAML = """\
available_tools:
  transform:
    implementation: "tests.test_tools.fake_transform"
    policy:
      timeout: 30
      retries: 0
"""

LEGIO_YAML = """\
node:
  id: "cli@test"
database:
  db_path: "{db_path}"
patterns:
  tool: "{tool_dir}"
  linguistic: "{linguistic_dir}"
  composite: "{composite_dir}"
tools:
  config: "{tools_file}"
lifecycle:
  default:
    drain_timeout: 10.0
    drain_interval: 0.02
"""


@pytest.fixture
def node_dirs(tmp_path: Path) -> dict[str, str]:
    """A tool-only node filesystem (the validation case, §7): one tool pattern,
    the Schema 3 tools file, and the LEG-017 config."""
    tool_dir = tmp_path / "patterns" / "tool"
    linguistic_dir = tmp_path / "patterns" / "linguistic"
    composite_dir = tmp_path / "patterns" / "composite"
    tool_dir.mkdir(parents=True)
    linguistic_dir.mkdir(parents=True)
    composite_dir.mkdir(parents=True)
    (tool_dir / "transform.yaml").write_text(TRANSFORM_YAML, encoding="utf-8")
    tools_file = tmp_path / "tools.yaml"
    tools_file.write_text(TOOLS_YAML, encoding="utf-8")
    db_path = tmp_path / "node.db"
    legio_yaml = tmp_path / "legio.yaml"
    legio_yaml.write_text(
        LEGIO_YAML.format(
            db_path=db_path,
            tool_dir=tool_dir,
            linguistic_dir=linguistic_dir,
            composite_dir=composite_dir,
            tools_file=tools_file,
        ),
        encoding="utf-8",
    )
    return {
        "config": str(legio_yaml),
        "db": str(db_path),
        "spec": str(tool_dir / "transform.yaml"),
    }


def _agent(node_dirs: dict[str, str], command: str, **options: object) -> list[str]:
    """One ``legio agent <command>`` invocation: boot in-process, verb, teardown."""
    loaded = load(node_dirs["config"])
    return asyncio.run(agent_command(loaded, command, **options))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --- config loading (LEG-017 schema + precedence) ------------------------------


def test_cli_explicit_missing_config_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "legio.yaml")


def test_cli_overrides_win_over_file(node_dirs: dict[str, str], tmp_path: Path) -> None:
    other_db = tmp_path / "cli-node.db"
    loaded = load(
        node_dirs["config"],
        overrides=CliOverrides(node="cli@other", db_path=other_db),
    )
    assert loaded.config.node.id == "cli@other"
    assert loaded.config.database.db_path == other_db


# --- executor ownership --------------------------------------------------------


def test_cli_executor_fleet_sizes_from_served_pools_plus_spares(
    node_dirs: dict[str, str],
) -> None:
    loaded = load(node_dirs["config"])

    async def _boot_and_size() -> int:
        booted = await boot_node(loaded)
        try:
            return executor_pump_count(booted)
        finally:
            await booted.db.close()

    count = asyncio.run(_boot_and_size())
    # transform pool resolves to 1 (default) → 1 + 3 spares, floored at 3.
    assert count == 4


# --- agent verbs: create/recreate/instances (direct Runtime verbs) -------------


def test_cli_agent_create_class_births_served_class(node_dirs: dict[str, str]) -> None:
    lines = _agent(node_dirs, "create-class", spec=Path(node_dirs["spec"]), pool=1)
    assert lines == ["class created name=transform pool=1"]
    assert _agent(node_dirs, "class-state", name="transform") == ["enabled"]
    listing = _agent(node_dirs, "list-classes")
    assert listing == ["transform\ttool\tenabled"]


def test_cli_agent_create_class_unserved_spec_fails_loudly(
    node_dirs: dict[str, str], tmp_path: Path
) -> None:
    ghost = tmp_path / "ghost.yaml"
    ghost.write_text(TRANSFORM_YAML.replace("name: transform", "name: ghost"), encoding="utf-8")
    with pytest.raises(LegioError, match="no standing agent"):
        _agent(node_dirs, "create-class", spec=ghost, pool=1)


def test_cli_agent_create_instances_seeded_over_recorded(
    node_dirs: dict[str, str],
) -> None:
    _agent(node_dirs, "create-class", spec=Path(node_dirs["spec"]), pool=1)
    lines = _agent(node_dirs, "create-instance", name="transform", count=2)
    assert len(lines) == 2
    assert lines[0].startswith("instance created class=transform id=transform-")
    assert len(_agent(node_dirs, "list-instances", name="transform")) == 3


def test_cli_agent_destroy_then_recreate_from_cached_yaml(
    node_dirs: dict[str, str],
) -> None:
    _agent(node_dirs, "create-class", spec=Path(node_dirs["spec"]), pool=0)
    assert _agent(node_dirs, "destroy-class", name="transform") == [
        "destroy class transform mode=drain ok"
    ]
    assert _agent(node_dirs, "class-state", name="transform") == ["absent"]
    assert _agent(node_dirs, "recreate-class", name="transform", pool=1) == [
        "class recreated name=transform pool=1"
    ]
    assert _agent(node_dirs, "class-state", name="transform") == ["enabled"]


# --- agent verbs: enable/disable/destroy (node-op intake) ----------------------


def test_cli_agent_pool_zero_born_disabled_then_enable_converges(
    node_dirs: dict[str, str],
) -> None:
    assert _agent(node_dirs, "create-class", spec=Path(node_dirs["spec"]), pool=0) == [
        "class created name=transform pool=0"
    ]
    assert _agent(node_dirs, "class-state", name="transform") == ["disabled"]
    assert _agent(node_dirs, "enable-class", name="transform") == ["enable class transform ok"]
    assert _agent(node_dirs, "class-state", name="transform") == ["enabled"]
    assert len(_agent(node_dirs, "list-instances", name="transform")) == 1


def test_cli_agent_disable_class_is_instant_record(node_dirs: dict[str, str]) -> None:
    _agent(node_dirs, "create-class", spec=Path(node_dirs["spec"]), pool=1)
    assert _agent(node_dirs, "disable-class", name="transform") == ["disable class transform ok"]
    assert _agent(node_dirs, "class-state", name="transform") == ["disabled"]


def test_cli_agent_reads_delegate_to_registry_mirror(
    node_dirs: dict[str, str],
) -> None:
    _agent(node_dirs, "create-class", spec=Path(node_dirs["spec"]), pool=1)
    assert _agent(node_dirs, "class-deps", name="transform") == ["absent"]
    assert _agent(node_dirs, "class-dependents", name="transform") == ["absent"]


def test_cli_agent_unknown_verbs_fail_loudly(node_dirs: dict[str, str]) -> None:
    with pytest.raises(LegioError, match="unknown class"):
        _agent(node_dirs, "disable-class", name="nope")
    with pytest.raises(LegioError, match="unknown class"):
        _agent(node_dirs, "destroy-class", name="nope")
    assert _agent(node_dirs, "class-state", name="nope") == ["absent"]


# --- the server surface in-process (§8 bootstrap + submit/status) -------------


@pytest.mark.asyncio
async def test_cli_server_surface_serves_submit_status(
    node_dirs: dict[str, str],
) -> None:
    loaded = load(node_dirs["config"])
    booted = await boot_node(loaded)
    stop = asyncio.Event()
    pumps = [
        asyncio.create_task(executor_loop(booted.runtime, stop.is_set))
        for _ in range(executor_pump_count(booted))
    ]
    try:
        await bring_catalog_up(booted)
        classes = await booted.runtime.list_classes()
        assert any(class_entry.name == "transform" for class_entry in classes)

        transport = httpx.ASGITransport(app=booted.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://cli") as ac:
            resp = await ac.post(
                "/submit",
                json={
                    "client_id": "cli-test",
                    "agent": "transform",
                    "payload": {"text": "hello", "factor": 2},
                },
            )
            assert resp.status_code == 200, resp.text
            task_id = resp.json()["task_id"]
            deadline = time.monotonic() + 10
            entry: dict = {}
            while time.monotonic() < deadline:
                status_resp = await ac.get(f"/status/{task_id}", params={"client_id": "cli-test"})
                assert status_resp.status_code == 200, status_resp.text
                entry = status_resp.json()
                if entry["state"] == "completed":
                    break
                await asyncio.sleep(0.02)
            assert entry["state"] == "completed"
            assert entry["output"] == {"transform": {"transformed": "HELLOHELLO"}}
            assert entry["result_key"] == outbox_key(task_id)
    finally:
        await _shutdown_pumps(pumps, stop)
        await booted.db.close()


def test_cli_server_starts_then_sigterm_drains_and_returns(
    node_dirs: dict[str, str],
) -> None:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "legio",
            "server",
            "--config",
            node_dirs["config"],
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ},
    )
    try:
        _await_http_up(base, deadline=20)
        client = httpx.Client(base_url=base, timeout=5)
        try:
            resp = client.post(
                "/submit",
                json={
                    "client_id": "cli-subproc",
                    "agent": "transform",
                    "payload": {"text": "hello", "factor": 3},
                },
            )
            assert resp.status_code == 200, resp.text
            task_id = resp.json()["task_id"]
            entry = _await_status(client, task_id, deadline=20)
            assert entry["state"] == "completed"
            assert entry["output"] == {"transform": {"transformed": "HELLOHELLOHELLO"}}
        finally:
            client.close()

        process.send_signal(signal.SIGTERM)
        returncode = process.wait(timeout=20)
        assert process.stdout is not None
        assert returncode == 0, process.stdout.read()

        # The drained outcome is durable: the outbox record survived the exit.
        record = _read_outbox(node_dirs["db"], task_id)
        assert record is not None
        assert record["payload"] == {"transform": {"transformed": "HELLOHELLOHELLO"}}
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_cli_server_federation_without_token_refuses_before_boot(
    node_dirs: dict[str, str],
) -> None:
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("LEGIO_")}
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "legio",
            "server",
            "--config",
            node_dirs["config"],
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--federation",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    try:
        returncode = process.wait(timeout=30)
        assert process.stdout is not None
        output = process.stdout.read()
        assert returncode == 1, output
        assert "LEGIO_FEDERATION_TOKEN" in output
        # Refusal happens before boot: the node database is never created.
        assert not Path(node_dirs["db"]).exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


# --- helpers -------------------------------------------------------------------


def _await_http_up(base: str, deadline: float) -> None:
    """Server-up probe: *any* HTTP response (even 404 on a missing task) means
    uvicorn is accepting connections."""
    client = httpx.Client(base_url=base, timeout=2)
    try:
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            try:
                response = client.get("/status/nope")
            except httpx.HTTPError:
                time.sleep(0.1)
                continue
            assert response.status_code in (200, 404), response.text
            return
        raise AssertionError(f"server did not come up within {deadline}s")
    finally:
        client.close()


def _await_status(client: httpx.Client, task_id: str, deadline: float) -> dict:
    end = time.monotonic() + deadline
    entry: dict = {}
    while time.monotonic() < end:
        response = client.get(f"/status/{task_id}", params={"client_id": "cli-subproc"})
        assert response.status_code == 200, response.text
        entry = response.json()
        if entry["state"] == "completed":
            return entry
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not complete within {deadline}s: {entry}")


def _read_outbox(db_path: str, task_id: str) -> dict | None:
    async def _run() -> dict | None:
        db = AsyncBeaverDB(db_path)
        await db.connect()
        try:
            return await db.dict("outbox").fetch(task_id)
        finally:
            await db.close()

    return asyncio.run(_run())
