"""#59 — a node restarted against an existing database must serve again.

The catalog registry persists in beaver while an agent's instance (its
standing loop) is process-bound (LEG-087/095). On restart the class is still
recorded, so a boot that treats "already recorded" as "already brought up"
leaves the node with no live consumer: a fresh submit stays `running` forever.
This test boots, stops, and boots again against the SAME db, then asserts the
round-trip completes.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from legio.cli import _shutdown_pumps, bring_catalog_up, executor_loop, executor_pump_count
from legio.config import load
from legio.materializer import boot_node
from legio.runtime import TaskState


def _write_node(tmp_path: Path) -> Path:
    tool_dir = tmp_path / "tool"
    tool_dir.mkdir()
    (tool_dir / "ok.yaml").write_text(
        """name: ok
type: atomic
kind: tool
main: true
input:
  input_as: ok
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
      factor: {type: integer}
output:
  output_as: ok
  output_type: json
  output_schema:
    type: object
    properties:
      transformed: {type: string}
tool: ok_tool
parameters:
  text: "{ok.text}"
  factor: "{ok.factor}"
""",
        encoding="utf-8",
    )
    for empty in ("linguistic", "composite"):
        (tmp_path / empty).mkdir()
    (tmp_path / "tools.yaml").write_text(
        """available_tools:
  ok_tool:
    implementation: tests.tools.transform
    policy:
      timeout: 10
      retries: 0
""",
        encoding="utf-8",
    )
    config = tmp_path / "legio.yaml"
    config.write_text(
        f"""node:
  id: "restart@local"
database:
  db_path: "{tmp_path / "node.db"}"
patterns:
  tool: "{tool_dir}"
  linguistic: "{tmp_path / "linguistic"}"
  composite: "{tmp_path / "composite"}"
tools:
  config: "{tmp_path / "tools.yaml"}"
lifecycle:
  default:
    drain_timeout: 5.0
    drain_interval: 0.01
""",
        encoding="utf-8",
    )
    return config


async def _boot_and_roundtrip(config: Path, text: str) -> dict:
    booted = await boot_node(load(config))
    stop = asyncio.Event()
    pumps = [
        asyncio.create_task(executor_loop(booted.runtime, stop.is_set))
        for _ in range(executor_pump_count(booted))
    ]
    try:
        await bring_catalog_up(booted)
        task_id = await booted.runtime.submit("demo", (("ok", "ok"),), {"text": text, "factor": 2})
        deadline = time.monotonic() + 10
        entry = await booted.runtime.status(task_id, "demo")
        while time.monotonic() < deadline and entry.state not in (
            TaskState.COMPLETED,
            TaskState.FAILED,
        ):
            await asyncio.sleep(0.02)
            entry = await booted.runtime.status(task_id, "demo")
        return {"state": entry.state, "output": entry.output}
    finally:
        await _shutdown_pumps(pumps, stop)
        await booted.db.close()


@pytest.mark.asyncio
async def test_restart_against_existing_db_serves_again(tmp_path: Path) -> None:
    config = _write_node(tmp_path)

    first = await _boot_and_roundtrip(config, "one")
    assert first["state"] == TaskState.COMPLETED, first
    assert first["output"] == {"ok": {"transformed": "ONEONE"}}, first

    # Restart on the same database file: the class is still recorded, but its
    # process-bound instance is gone. The boot must bring a live consumer back.
    second = await _boot_and_roundtrip(config, "two")
    assert second["state"] == TaskState.COMPLETED, second
    assert second["output"] == {"ok": {"transformed": "TWOTWO"}}, second
