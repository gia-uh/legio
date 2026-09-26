"""LEG-105 — the task state reflects the flow outcome (red-first contract tests).

A flow whose step raised must report ``state == failed`` (with the error still
visible in ``output``); a clean flow reports ``completed``. The state is
derived from the outbox ``ExecutionResultMessage`` (the flow outcome), not from
the seed task record, which only reflects the root-token dispatch.
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

FAIL_PATTERN = """name: fail
type: atomic
kind: tool
main: true
input:
  input_as: in
  input_type: json
  input_schema: {}
output:
  output_as: out
  output_type: json
  output_schema: {}
tool: failing_tool
parameters: {}
"""

OK_PATTERN = """name: ok
type: atomic
kind: tool
main: true
input:
  input_as: in
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
  text: "{in.text}"
  factor: "{in.factor}"
"""

TOOLS_YAML = """available_tools:
  failing_tool:
    implementation: tests.tools.failing_tool
    policy:
      timeout: 10
      retries: 0
  ok_tool:
    implementation: tests.tools.transform
    policy:
      timeout: 10
      retries: 0
"""


async def _boot(tmp_path: Path):
    tool_dir = tmp_path / "tool"
    tool_dir.mkdir()
    (tool_dir / "fail.yaml").write_text(FAIL_PATTERN, encoding="utf-8")
    (tool_dir / "ok.yaml").write_text(OK_PATTERN, encoding="utf-8")
    for empty in ("linguistic", "composite"):
        (tmp_path / empty).mkdir()
    (tmp_path / "tools.yaml").write_text(TOOLS_YAML, encoding="utf-8")
    config = tmp_path / "legio.yaml"
    config.write_text(
        f"""node:
  id: "test@local"
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
    booted = await boot_node(load(config))
    stop = asyncio.Event()
    pumps = [
        asyncio.create_task(executor_loop(booted.runtime, stop.is_set))
        for _ in range(executor_pump_count(booted))
    ]
    await bring_catalog_up(booted)
    return booted, pumps, stop


async def _poll(runtime, task_id: str):
    deadline = time.monotonic() + 10
    entry = None
    while time.monotonic() < deadline:
        entry = await runtime.status(task_id, "client")
        if entry.state in (TaskState.COMPLETED, TaskState.FAILED):
            return entry
        await asyncio.sleep(0.02)
    raise AssertionError(f"task {task_id} did not reach a terminal state: {entry}")


@pytest.mark.asyncio
async def test_errored_flow_reports_failed(tmp_path: Path) -> None:
    booted, pumps, stop = await _boot(tmp_path)
    try:
        task_id = await booted.runtime.submit("client", (("fail", "in"),), {})
        entry = await _poll(booted.runtime, task_id)

        assert entry.state == TaskState.FAILED
        assert entry.output is not None
        assert "error" in entry.output, entry.output
    finally:
        await _shutdown_pumps(pumps, stop)
        await booted.db.close()


@pytest.mark.asyncio
async def test_clean_flow_reports_completed(tmp_path: Path) -> None:
    booted, pumps, stop = await _boot(tmp_path)
    try:
        task_id = await booted.runtime.submit(
            "client", (("ok", "in"),), {"text": "hi", "factor": 2}
        )
        entry = await _poll(booted.runtime, task_id)

        assert entry.state == TaskState.COMPLETED
        assert entry.output == {"ok": {"transformed": "HIHI"}}
    finally:
        await _shutdown_pumps(pumps, stop)
        await booted.db.close()
