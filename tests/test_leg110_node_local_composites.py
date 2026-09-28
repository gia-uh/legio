"""LEG-110 — node-local composite classes: a composite node boots from config.

Red-first contract tests for the defect: a node containing a ``type: composite``
pattern could not boot through the CLI because nothing supplied the concrete
composite classes. Now the node config declares a ``composites.config`` module
that exposes ``COMPOSITE_CLASSES``; ``boot_node`` loads and injects it.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import socket
import subprocess
import sys
import time

import pytest
from beaver import AsyncBeaverDB
from lingo.mock import MockLLM

from legio.agents import CompositeAgent
from legio.config import load
from legio.errors import ConfigError
from legio.materializer import boot_node, load_composite_classes

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPOSITE_EXAMPLES = (
    "summarize",
    "extract-and-summarize",
    "distribute-summary",
    "document_processing",
)
# The composite pattern each example boots (name -> example dir).
EXAMPLE_COMPOSITES = {
    "summarize": "summarize",
    "extract-and-summarize": "extract_and_summarize",
    "distribute-summary": "distribute_summary",
    "document_processing": "doc_pipeline",
}


def _mock_lingo_factory(_llm: object, _api_key: object) -> MockLLM:
    """A no-network lingo client: boot only, no calls are made."""
    return MockLLM(responses=[])


def _legio_script() -> pathlib.Path:
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


def test_load_composite_classes_reads_the_mapping(tmp_path: pathlib.Path) -> None:
    module = tmp_path / "composites.py"
    module.write_text(
        "from legio.agents import CompositeAgent\n"
        "class _C(CompositeAgent):\n"
        "    async def build_output_as(self, info):\n"
        "        return {}\n"
        "COMPOSITE_CLASSES = {'flow': _C}\n",
        encoding="utf-8",
    )
    classes = load_composite_classes(module)
    assert classes == {"flow": classes["flow"]}
    assert issubclass(classes["flow"], CompositeAgent)


def test_load_composite_classes_without_mapping_fails_loudly(tmp_path: pathlib.Path) -> None:
    module = tmp_path / "composites.py"
    module.write_text("# no COMPOSITE_CLASSES\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_composite_classes(module)


@pytest.mark.asyncio
async def test_boot_node_loads_composites_from_config(
    beaver_db: AsyncBeaverDB,
) -> None:
    """No injected classes: the config's composites module supplies them."""
    loaded = load(REPO_ROOT / "examples" / "summarize" / "legio.yaml")
    booted = await boot_node(loaded, db=beaver_db, lingo_factory=_mock_lingo_factory)
    assert "summarize" in booted.agents
    assert isinstance(booted.agents["summarize"], CompositeAgent)


@pytest.mark.asyncio
async def test_declared_missing_composites_module_fails_loudly(
    tmp_path: pathlib.Path,
) -> None:
    config = tmp_path / "legio.yaml"
    config.write_text(
        'node:\n  id: "x@y"\n'
        'database:\n  db_path: "x.db"\n'
        'patterns:\n  tool: "./patterns/tool"\n'
        '  linguistic: "./patterns/linguistic"\n'
        '  composite: "./patterns/composite"\n'
        'composites:\n  config: "./does-not-exist.py"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        await boot_node(load(config))


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", COMPOSITE_EXAMPLES)
async def test_every_composite_example_boots_from_config(
    flow: str, beaver_db: AsyncBeaverDB
) -> None:
    """Every shipped composite example materializes with no injected classes."""
    loaded = load(REPO_ROOT / "examples" / flow / "legio.yaml")
    booted = await boot_node(loaded, db=beaver_db, lingo_factory=_mock_lingo_factory)
    composite_name = EXAMPLE_COMPOSITES[flow]
    assert composite_name in booted.agents
    assert isinstance(booted.agents[composite_name], CompositeAgent)


def test_composite_node_boots_via_cli(tmp_path: pathlib.Path) -> None:
    """The documented command boots a composite node (the defect's regression)."""
    shutil.rmtree(REPO_ROOT / "examples" / "summarize" / "db", ignore_errors=True)
    port = _free_port()
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["LEGIO_LLM_API_KEY"] = "dummy"  # a local endpoint still needs a value
    log_path = tmp_path / "server.log"
    with log_path.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            [
                str(_legio_script()),
                "server",
                "--config",
                "examples/summarize/legio.yaml",
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
    try:
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
        assert ready, f"composite node did not boot; log:\n{log_path.read_text()}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
