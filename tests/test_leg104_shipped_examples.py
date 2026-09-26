"""LEG-104 — the shipped examples run exactly as documented.

Red-first contract tests for the audit finding 1: the documented command
(`uv run legio server --config examples/transform/legio.yaml` from the repo
root, with no ``PYTHONPATH`` and no manual ``mkdir``) must boot, serve a
submit → status round-trip and return the documented output.

The subprocess test deliberately invokes the **installed console script** (not
``python -m`` / pytest), so a regression to a repo-root-relative tool path or a
CWD-relative config path fails it — the check `tests/test_leg100` lacked.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import replace

import httpx
import pytest

from legio.cli import validate_node
from legio.config import load, resolve_config_paths
from legio.materializer import available_tools_from_config

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
NODE_FLOWS = (
    "transform",
    "summarize",
    "extract-and-summarize",
    "distribute-summary",
    "document_processing",
)


def _legio_script() -> pathlib.Path:
    """The installed `legio` console script (the way a consumer runs it)."""
    candidate = pathlib.Path(sys.executable).with_name("legio")
    if candidate.is_file():
        return candidate
    found = shutil.which("legio")
    if found:
        return pathlib.Path(found)
    pytest.skip("legio console script not found on PATH")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_every_shipped_config_resolves_and_tools_load() -> None:
    """Every shipped ``legio.yaml`` resolves its paths against the config file
    and every declared tool resolves through the node-local loader."""
    for flow in NODE_FLOWS:
        node = REPO_ROOT / "examples" / flow
        loaded = load(node / "legio.yaml")
        resolved = resolve_config_paths(loaded)
        for kind in ("tool", "linguistic", "composite"):
            directory = getattr(resolved.patterns, kind)
            assert directory.is_dir(), f"{flow}: {kind} dir missing: {directory}"

        registry = available_tools_from_config(replace(loaded, config=resolved))
        for name in registry.all_declarations():
            assert callable(registry.load_tool(name)), f"{flow}: {name} tool does not load"


@pytest.mark.asyncio
async def test_every_shipped_config_validates_clean() -> None:
    """`legio validate` accepts every shipped node from the repo root."""
    for flow in NODE_FLOWS:
        code = await validate_node(load(REPO_ROOT / "examples" / flow / "legio.yaml"))
        assert code == 0, f"{flow}: shipped config is invalid"


def test_shipped_transform_node_runs_as_documented(tmp_path: pathlib.Path) -> None:
    """The README command, run verbatim from the repo root, round-trips."""
    # The node's `db/` is runtime state (gitignored, created at boot): start
    # from a clean node, as a fresh clone does.
    shutil.rmtree(REPO_ROOT / "examples" / "transform" / "db", ignore_errors=True)
    port = _free_port()
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)  # the consumer has no repo root on sys.path
    log_path = tmp_path / "server.log"
    with log_path.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            [
                str(_legio_script()),
                "server",
                "--config",
                "examples/transform/legio.yaml",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
    base = f"http://127.0.0.1:{port}"
    try:
        # `/health` is only mounted on the federation surface; probe the TCP
        # bind (the server is ready once it accepts connections).
        deadline = time.monotonic() + 60
        ready = False
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    ready = True
                    break
            except OSError:
                pass
            time.sleep(0.1)
        if not ready:
            raise AssertionError(f"server not ready; log:\n{log_path.read_text()}")

        submitted = httpx.post(
            f"{base}/submit",
            json={
                "client_id": "demo",
                "agent": "transform",
                "payload": {"text": "hello", "factor": 2},
            },
            timeout=30,
        )
        assert submitted.status_code == 200, submitted.text
        task_id = submitted.json()["task_id"]

        entry: dict = {}
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = httpx.get(f"{base}/status/{task_id}", params={"client_id": "demo"}, timeout=30)
            assert status.status_code == 200, status.text
            entry = status.json()
            if entry.get("state") in ("completed", "failed"):
                break
            time.sleep(0.05)

        assert entry.get("state") == "completed", f"{entry}\nlog:\n{log_path.read_text()}"
        assert entry.get("output") == {"transform": {"transformed": "HELLOHELLO"}}, entry
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
