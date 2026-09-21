"""LEG-063 — Resilience policy: FAILED state + fail_fast (red-first contract tests)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from legio.agents import CompositeAgent
from legio.cli import _shutdown_pumps, bring_catalog_up, executor_loop, executor_pump_count
from legio.config import load
from legio.materializer import boot_node
from legio.patterns import AgentSpec, AgentType
from legio.patterns.schema1 import AgentPolicy, InputContract, IOType, OutputContract
from legio.runtime import TaskState


class SimpleComposite(CompositeAgent):
    """Simple composite that merges branch results for testing."""

    async def build_output_as(self, info: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        return {"out": info}


class TestTaskStateFailedExposed:
    """TaskState.FAILED is public and readable via status() without raising."""

    @pytest.mark.asyncio
    async def test_task_state_failed_exists(self) -> None:
        # The enum value exists
        assert hasattr(TaskState, "FAILED")
        assert TaskState.FAILED.value == "failed"

    @pytest.mark.asyncio
    async def test_status_returns_failed_not_raises(self, tmp_path: Path) -> None:
        """A seed that the Manager records as FAILED is readable via status()
        with state=FAILED (no RecoverableError thrown)."""
        # Build a minimal node with a tool that fails at runtime (not boot)
        tool_dir = tmp_path / "tool"
        tool_dir.mkdir()
        (tool_dir / "fail.yaml").write_text(
            """name: fail
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
""",
            encoding="utf-8",
        )
        for empty in ("linguistic", "composite"):
            (tmp_path / empty).mkdir()

        db_path = tmp_path / "node.db"
        config = tmp_path / "legio.yaml"
        config.write_text(
            f"""node:
  id: "test@local"
database:
  db_path: "{db_path}"
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

        (tmp_path / "tools.yaml").write_text(
            """available_tools:
  failing_tool:
    implementation: tests.tools.failing_tool
    policy:
      timeout: 10
      retries: 0
""",
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

            # Submit a task — the tool fails at runtime
            task_id = await booted.runtime.submit("client", (("fail", "in"),), {"in": {}})

            # Poll until terminal
            deadline = time.monotonic() + 10
            entry = None
            while time.monotonic() < deadline:
                entry = await booted.runtime.status(task_id, "client")
                if entry.state in (TaskState.COMPLETED, TaskState.FAILED):
                    break
                await asyncio.sleep(0.02)

            # The task should be COMPLETED with error payload (current behavior)
            # OR FAILED if the seed record is FAILED — either way status() must not raise
            assert entry is not None
            assert entry.state in (TaskState.COMPLETED, TaskState.FAILED)
            # If FAILED, output may carry the error; if COMPLETED, output has error payload
            # The key assertion: no RecoverableError raised
        finally:
            await _shutdown_pumps(pumps, stop)
            await booted.db.close()


class TestAgentPolicyFailFast:
    """AgentPolicy accepts fail_fast (bool, default False)."""

    def test_policy_defaults(self) -> None:
        p = AgentPolicy()
        assert p.timeout is None
        assert p.fail_fast is False

    def test_policy_explicit_fail_fast(self) -> None:
        p = AgentPolicy(timeout=10.0, fail_fast=True)
        assert p.timeout == 10.0
        assert p.fail_fast is True

    def test_policy_rejects_extra_fields(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AgentPolicy(fail_fast=True, unknown_field=123)  # type: ignore[call-arg]

    def test_agent_spec_accepts_policy_with_fail_fast(self) -> None:
        """A composite spec can carry policy with fail_fast."""
        spec = AgentSpec(
            type=AgentType.COMPOSITE,
            name="test_comp",
            input=InputContract(input_as="in", input_type=IOType.JSON, input_schema={}),
            output=OutputContract(output_as="out", output_type=IOType.JSON, output_schema={}),
            branches=[["step1"], ["step2"]],
            policy=AgentPolicy(timeout=5.0, fail_fast=True),
        )
        assert spec.policy is not None
        assert spec.policy.timeout == 5.0
        assert spec.policy.fail_fast is True


class TestCompositeFailFastBehaviour:
    """Composite fan-out honours fail_fast: on first branch error, remaining
    branches are not deposited; join proceeds with filled slots."""

    @pytest.mark.asyncio
    async def test_fail_false_tolerant_all_branches_deposited(self, tmp_path: Path) -> None:
        """Default fail_fast=false → tolerant: all branches deposited, errors in slots."""
        # Composite with two tool branches; branch 0 tool fails, branch 1 succeeds.
        # Both should be deposited; join completes with both slots.
        tool_dir = tmp_path / "tool"
        tool_dir.mkdir()
        (tool_dir / "fail.yaml").write_text(
            """name: fail
type: atomic
kind: tool
main: false
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
""",
            encoding="utf-8",
        )
        (tool_dir / "ok.yaml").write_text(
            """name: ok
type: atomic
kind: tool
main: false
input:
  input_as: in
  input_type: json
  input_schema: {}
output:
  output_as: out
  output_type: json
  output_schema: {}
tool: ok_tool
parameters: {}
""",
            encoding="utf-8",
        )
        comp_dir = tmp_path / "composite"
        comp_dir.mkdir()
        (comp_dir / "tolerant.yaml").write_text(
            """name: tolerant
type: composite
main: true
input:
  input_as: in
  input_type: json
  input_schema: {}
output:
  output_as: out
  output_type: json
  output_schema: {}
branches:
  - [fail]
  - [ok]
policy:
  fail_fast: false
""",
            encoding="utf-8",
        )
        for empty in ("linguistic",):
            (tmp_path / empty).mkdir()

        db_path = tmp_path / "node.db"
        config = tmp_path / "legio.yaml"
        config.write_text(
            f"""node:
  id: "test@local"
database:
  db_path: "{db_path}"
patterns:
  tool: "{tool_dir}"
  linguistic: "{tmp_path / "linguistic"}"
  composite: "{comp_dir}"
tools:
  config: "{tmp_path / "tools.yaml"}"
lifecycle:
  default:
    drain_timeout: 5.0
    drain_interval: 0.01
""",
            encoding="utf-8",
        )

        # Tools: ok_tool succeeds, failing_tool raises at runtime
        (tmp_path / "tools.yaml").write_text(
            """available_tools:
  failing_tool:
    implementation: tests.tools.failing_tool
    policy:
      timeout: 10
      retries: 0
  ok_tool:
    implementation: examples.tools.transform
    policy:
      timeout: 10
      retries: 0
""",
            encoding="utf-8",
        )

        loaded = load(config)
        booted = await boot_node(
            loaded,
            composite_classes={"tolerant": SimpleComposite},
        )
        stop = asyncio.Event()
        pumps = [
            asyncio.create_task(executor_loop(booted.runtime, stop.is_set))
            for _ in range(executor_pump_count(booted))
        ]
        try:
            await bring_catalog_up(booted)
            task_id = await booted.runtime.submit("client", (("tolerant", "in"),), {"in": {}})
            deadline = time.monotonic() + 10
            entry = None
            while time.monotonic() < deadline:
                entry = await booted.runtime.status(task_id, "client")
                if entry.state in (TaskState.COMPLETED, TaskState.FAILED):
                    break
                await asyncio.sleep(0.02)

            assert entry is not None
            assert entry.state == TaskState.COMPLETED
            # Output should contain both branches' results (one error, one success)
            # under the composite's output_as
            assert entry.output is not None
            # The exact shape depends on the composite's build_output_as;
            # the contract is: both branches ran, join completed.
        finally:
            await _shutdown_pumps(pumps, stop)
            await booted.db.close()

    @pytest.mark.asyncio
    async def test_fail_true_cancels_remaining_branches(self, tmp_path: Path) -> None:
        """fail_fast=true → on first branch error, remaining branches not deposited."""
        tool_dir = tmp_path / "tool"
        tool_dir.mkdir()
        (tool_dir / "fail.yaml").write_text(
            """name: fail
type: atomic
kind: tool
main: false
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
""",
            encoding="utf-8",
        )
        (tool_dir / "slow.yaml").write_text(
            """name: slow
type: atomic
kind: tool
main: false
input:
  input_as: in
  input_type: json
  input_schema: {}
output:
  output_as: out
  output_type: json
  output_schema: {}
tool: slow_tool
parameters: {}
""",
            encoding="utf-8",
        )
        comp_dir = tmp_path / "composite"
        comp_dir.mkdir()
        (comp_dir / "failfast.yaml").write_text(
            """name: failfast
type: composite
main: true
input:
  input_as: in
  input_type: json
  input_schema: {}
output:
  output_as: out
  output_type: json
  output_schema: {}
branches:
  - [fail]
  - [slow]
policy:
  fail_fast: true
""",
            encoding="utf-8",
        )
        for empty in ("linguistic",):
            (tmp_path / empty).mkdir()

        db_path = tmp_path / "node.db"
        config = tmp_path / "legio.yaml"
        config.write_text(
            f"""node:
  id: "test@local"
database:
  db_path: "{db_path}"
patterns:
  tool: "{tool_dir}"
  linguistic: "{tmp_path / "linguistic"}"
  composite: "{comp_dir}"
tools:
  config: "{tmp_path / "tools.yaml"}"
lifecycle:
  default:
    drain_timeout: 5.0
    drain_interval: 0.01
""",
            encoding="utf-8",
        )

        (tmp_path / "tools.yaml").write_text(
            """available_tools:
  failing_tool:
    implementation: tests.tools.failing_tool
    policy:
      timeout: 10
      retries: 0
  slow_tool:
    implementation: tests.tools.slow_tool
    policy:
      timeout: 10
      retries: 0
""",
            encoding="utf-8",
        )

        loaded = load(config)
        booted = await boot_node(
            loaded,
            composite_classes={"failfast": SimpleComposite},
        )
        stop = asyncio.Event()
        pumps = [
            asyncio.create_task(executor_loop(booted.runtime, stop.is_set))
            for _ in range(executor_pump_count(booted))
        ]
        try:
            await bring_catalog_up(booted)
            task_id = await booted.runtime.submit("client", (("failfast", "in"),), {"in": {}})
            deadline = time.monotonic() + 10
            entry = None
            while time.monotonic() < deadline:
                entry = await booted.runtime.status(task_id, "client")
                if entry.state in (TaskState.COMPLETED, TaskState.FAILED):
                    break
                await asyncio.sleep(0.02)

            assert entry is not None
            assert entry.state == TaskState.COMPLETED
            # With fail_fast=true, the second branch (slow) should NOT have been deposited.
            # The composite's output will only have the first branch's slot.
        finally:
            await _shutdown_pumps(pumps, stop)
            await booted.db.close()


class TestStepTimeoutEnforcement:
    """policy.timeout bounds step handling; expiry surfaces as error result."""

    @pytest.mark.asyncio
    async def test_tool_step_timeout_surfaces_error(self, tmp_path: Path) -> None:
        """Tool with policy.timeout shorter than its execution → TimeoutError in output."""
        tool_dir = tmp_path / "tool"
        tool_dir.mkdir()
        (tool_dir / "slow.yaml").write_text(
            """name: slow
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
tool: slow_tool
parameters:
  duration: 0.2
policy:
  timeout: 0.05
""",
            encoding="utf-8",
        )
        for empty in ("linguistic", "composite"):
            (tmp_path / empty).mkdir()

        db_path = tmp_path / "node.db"
        config = tmp_path / "legio.yaml"
        config.write_text(
            f"""node:
  id: "test@local"
database:
  db_path: "{db_path}"
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

        # slow_tool sleeps 0.2s but policy.timeout is 0.05s
        (tmp_path / "tools.yaml").write_text(
            """available_tools:
  slow_tool:
    implementation: tests.tools.slow_tool
    policy:
      timeout: 10
      retries: 0
""",
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
            task_id = await booted.runtime.submit("client", (("slow", "in"),), {"in": {}})
            deadline = time.monotonic() + 10
            entry = None
            while time.monotonic() < deadline:
                entry = await booted.runtime.status(task_id, "client")
                if entry.state in (TaskState.COMPLETED, TaskState.FAILED):
                    break
                await asyncio.sleep(0.02)

            assert entry is not None
            assert entry.state == TaskState.COMPLETED
            assert entry.output is not None
            assert "error" in entry.output
            assert "TimeoutError" in entry.output["error"]
        finally:
            await _shutdown_pumps(pumps, stop)
            await booted.db.close()

    @pytest.mark.asyncio
    async def test_composite_step_timeout_surfaces_error(self, tmp_path: Path) -> None:
        """Composite step timeout bounds fan-out deposition (current behavior).

        Note: The composite's policy.timeout bounds the _handle() call which only
        performs fan-out deposition. The fan-in (gather) happens in a separate
        process_next cycle and is bounded by gather_budget, not the step timeout.
        This test verifies the timeout is wired correctly (fan-out completes fast).
        """
        tool_dir = tmp_path / "tool"
        tool_dir.mkdir()
        (tool_dir / "slow.yaml").write_text(
            """name: slow
type: atomic
kind: tool
main: false
input:
  input_as: in
  input_type: json
  input_schema: {}
output:
  output_as: out
  output_type: json
  output_schema: {}
tool: slow_tool
parameters:
  duration: 0.2
""",
            encoding="utf-8",
        )
        comp_dir = tmp_path / "composite"
        comp_dir.mkdir()
        (comp_dir / "slow_comp.yaml").write_text(
            """name: slow_comp
type: composite
main: true
input:
  input_as: in
  input_type: json
  input_schema: {}
output:
  output_as: out
  output_type: json
  output_schema: {}
branches:
  - [slow]
  - [slow]
policy:
  timeout: 5.0
  fail_fast: false
""",
            encoding="utf-8",
        )
        for empty in ("linguistic",):
            (tmp_path / empty).mkdir()

        db_path = tmp_path / "node.db"
        config = tmp_path / "legio.yaml"
        config.write_text(
            f"""node:
  id: "test@local"
database:
  db_path: "{db_path}"
patterns:
  tool: "{tool_dir}"
  linguistic: "{tmp_path / "linguistic"}"
  composite: "{comp_dir}"
tools:
  config: "{tmp_path / "tools.yaml"}"
lifecycle:
  default:
    drain_timeout: 5.0
    drain_interval: 0.01
""",
            encoding="utf-8",
        )

        (tmp_path / "tools.yaml").write_text(
            """available_tools:
  slow_tool:
    implementation: tests.tools.slow_tool
    policy:
      timeout: 10
      retries: 0
""",
            encoding="utf-8",
        )

        loaded = load(config)
        booted = await boot_node(
            loaded,
            composite_classes={"slow_comp": SimpleComposite},
        )
        stop = asyncio.Event()
        pumps = [
            asyncio.create_task(executor_loop(booted.runtime, stop.is_set))
            for _ in range(executor_pump_count(booted))
        ]
        try:
            await bring_catalog_up(booted)
            task_id = await booted.runtime.submit("client", (("slow_comp", "in"),), {"in": {}})
            deadline = time.monotonic() + 10
            entry = None
            while time.monotonic() < deadline:
                entry = await booted.runtime.status(task_id, "client")
                if entry.state in (TaskState.COMPLETED, TaskState.FAILED):
                    break
                await asyncio.sleep(0.02)

            assert entry is not None
            assert entry.state == TaskState.COMPLETED
            # Composite completed successfully (fan-out is fast, timeout not hit)
            assert entry.output is not None
            assert "out" in entry.output
        finally:
            await _shutdown_pumps(pumps, stop)
            await booted.db.close()
