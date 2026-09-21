"""LEG-100 — consumer guide walkthrough: the guide is executable top-to-bottom.

Contract tests for the docs & examples hardening (R-10 release track). Three
proofs, all against the *same* files the consumer guide documents:

1. Every example node's patterns load through the real loader and every
   composite's branches resolve to loaded steps (drift guard).
2. Every example node's ``legio.yaml`` parses and its ``tools.yaml`` declares
   exactly the tool step patterns it ships, each implementation importable.
3. The headless ``transform`` example node **boots** and serves a submit →
   status round-trip over the REST surface — the executable core of the guide,
   with no LLM or composite seam.
"""

from __future__ import annotations

import asyncio
import importlib
import pathlib
import time

import httpx
import pytest

from legio.cli import _shutdown_pumps, bring_catalog_up, executor_loop, executor_pump_count
from legio.config import load, load_tools_file
from legio.materializer import boot_node
from legio.patterns import load_pattern_dirs, resolve_composite_branches

EXAMPLES = pathlib.Path(__file__).resolve().parents[1] / "examples"
NODE_FLOWS = ("transform", "summarize", "extract-and-summarize", "distribute-summary")


def _example_node(flow: str) -> pathlib.Path:
    return EXAMPLES / flow


def _pattern_dirs(flow: str) -> dict[str, pathlib.Path]:
    node = _example_node(flow)
    return {
        "tool": node / "patterns" / "tool",
        "linguistic": node / "patterns" / "linguistic",
        "composite": node / "patterns" / "composite",
    }


def test_guide_example_patterns_load_and_composites_resolve() -> None:
    """Every pattern the guide documents loads; every composite branch
    resolves to a loaded step — documented examples can never bitrot."""
    for flow in NODE_FLOWS:
        catalog = load_pattern_dirs(_pattern_dirs(flow))
        assert catalog.specs, f"{flow}: example node shipped no patterns"
        for name, spec in catalog.specs.items():
            if spec.type.value == "composite":
                routes = resolve_composite_branches(spec, catalog)
                assert routes, f"{flow}: composite {name} resolves to no branch"
                for branch in routes:
                    for step, _input_as in branch:
                        assert step in catalog.specs, (
                            f"{flow}: branch step {step!r} of {name!r} is not a loaded spec"
                        )


def test_guide_every_example_config_template_parses_and_tools_match() -> None:
    """Each example legio.yaml template parses, and its tools.yaml declares
    exactly the tool steps its patterns ship, each implementation importable."""
    for flow in NODE_FLOWS:
        node = _example_node(flow)
        loaded = load(node / "legio.yaml")
        assert loaded.config.node.id.endswith("@example")
        assert loaded.config.patterns.tool == pathlib.Path("patterns/tool")

        tools = load_tools_file(node / "tools.yaml")
        tool_patterns = {
            path.stem
            for path in (node / "patterns" / "tool").glob("*.yaml")
            if not path.name.startswith(".")
        }
        assert set(tools.available_tools) == tool_patterns, (
            f"{flow}: tools.yaml does not cover the shipped tool patterns"
        )
        for name, declaration in tools.available_tools.items():
            module_path, _, attr = declaration.implementation.rpartition(".")
            assert importlib.import_module(module_path), f"{flow}: {name} module"
            assert callable(getattr(importlib.import_module(module_path), attr))


@pytest.mark.asyncio
async def test_guide_transform_node_boots_and_serves_submit_status(
    tmp_path: pathlib.Path,
) -> None:
    """The guide's headless step — boot the transform node, submit, poll
    status — runs for real over the REST surface (executable core)."""
    node = _example_node("transform")
    tool_dir = node / "patterns" / "tool"
    tools_config = node / "tools.yaml"
    for empty in ("linguistic", "composite"):
        (tmp_path / empty).mkdir()
    db_path = tmp_path / "node.db"
    config = tmp_path / "legio.yaml"
    config.write_text(
        (
            f'node:\n  id: "cli@test"\n'
            f'database:\n  db_path: "{db_path}"\n'
            f'patterns:\n  tool: "{tool_dir}"\n'
            f'  linguistic: "{tmp_path / "linguistic"}"\n'
            f'  composite: "{tmp_path / "composite"}"\n'
            f'tools:\n  config: "{tools_config}"\n'
            "lifecycle:\n  default:\n    drain_timeout: 10.0\n"
            f"    drain_interval: 0.02\n"
        ),
        encoding="utf-8",
    )

    loaded = load(config)
    booted = await boot_node(loaded)
    stop = asyncio.Event()
    pumps = [
        asyncio.create_task(executor_loop(booted.runtime, stop.is_set))
        for _ in range(executor_pump_count(booted))
    ]
    try:
        await bring_catalog_up(booted)
        classes = await booted.runtime.list_classes()
        assert any(entry.name == "transform" for entry in classes)

        transport = httpx.ASGITransport(app=booted.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://example") as ac:
            resp = await ac.post(
                "/submit",
                json={
                    "client_id": "demo",
                    "agent": "transform",
                    "payload": {"text": "hello", "factor": 2},
                },
            )
            assert resp.status_code == 200, resp.text
            task_id = resp.json()["task_id"]
            deadline = time.monotonic() + 10
            entry: dict = {}
            while time.monotonic() < deadline:
                status_resp = await ac.get(f"/status/{task_id}", params={"client_id": "demo"})
                assert status_resp.status_code == 200, status_resp.text
                entry = status_resp.json()
                if entry["state"] == "completed":
                    break
                await asyncio.sleep(0.02)
            assert entry["state"] == "completed"
            assert entry["output"] == {"transform": {"transformed": "HELLOHELLO"}}
    finally:
        await _shutdown_pumps(pumps, stop)
        await booted.db.close()
