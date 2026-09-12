"""Contract tests for LEG-082 — concurrency semaphores + cooperative drain.

Per-tool and per-LLM concurrency caps are honoured under load (slow fake
tools): a constrained resource waits rather than failing or dropping work
(wait-not-fail). A drain request — set directly on ``ShutdownGate`` or by a
real SIGTERM via ``install_signal_drain`` — stops the pulling of *new* items
**between dispatches** (§12.2), while in-flight steps keep running and always
deposit their result.

The engine never sleeps and never polls a scheduling field (rule 8): the
semaphore acquire is a cooperative wait — the documented second rule-8
exception, like the bounded clock waits of §5.8. The slow fakes below DO sleep,
as "a slow fake tool" requires, because test fakes emulate an expensive local
resource, not the engine.
"""

from __future__ import annotations

import asyncio
import os
import signal
import threading
import time
from typing import Any

import pytest
from beaver import AsyncBeaverDB
from pydantic import BaseModel

from legio.agents.linguistic_agent import LinguisticAgent
from legio.agents.tool_agent import ToolAgent
from legio.concurrency import ConcurrencyCaps, ShutdownGate, install_signal_drain
from legio.config import load, load_tools_file
from legio.errors import ConfigError
from legio.flow import ExecutionRequestMessage, ExecutionResultMessage
from legio.materializer import materialize_agents
from legio.naming import queue_key
from legio.patterns import load_patterns
from legio.tools import AvailableToolsRegistry

# --- instrumentation shared by the slow fakes ---------------------------------


class PeakGauge:
    """Peak-and-current concurrency counter for the slow fakes.

    The tool fake runs in a worker thread (``asyncio.to_thread``), so the
    counter is guarded by a lock and ``entered`` is a thread-safe
    ``threading.Event``.
    """

    def __init__(self) -> None:
        self.concurrent = 0
        self.max_seen = 0
        self.entered: threading.Event | None = None
        self.delay = 0.2
        self._lock = threading.Lock()

    def reset(self, *, delay: float = 0.2) -> None:
        self.concurrent = 0
        self.max_seen = 0
        self.entered = threading.Event()
        self.delay = delay


TOOL_GAUGE = PeakGauge()
LLM_GAUGE = PeakGauge()


def fake_slow_tool(text: str, factor: int = 2) -> dict:
    """A genuinely slow, blocking tool (emulates an expensive local resource)."""
    if TOOL_GAUGE.entered is not None:
        TOOL_GAUGE.entered.set()
    with TOOL_GAUGE._lock:
        TOOL_GAUGE.concurrent += 1
        TOOL_GAUGE.max_seen = max(TOOL_GAUGE.max_seen, TOOL_GAUGE.concurrent)
    time.sleep(TOOL_GAUGE.delay)
    with TOOL_GAUGE._lock:
        TOOL_GAUGE.concurrent -= 1
    return {"transformed": text.upper()}


class SummarizeOutput(BaseModel):
    title: str
    summary: str
    word_count: int = 0


class BlockingLLM:
    """A lingo fake whose ``create`` is slow and counted (async sleep)."""

    async def create(self, model: Any, messages: Any) -> SummarizeOutput:
        LLM_GAUGE.concurrent += 1
        LLM_GAUGE.max_seen = max(LLM_GAUGE.max_seen, LLM_GAUGE.concurrent)
        await asyncio.sleep(LLM_GAUGE.delay)
        LLM_GAUGE.concurrent -= 1
        return SummarizeOutput(title="t", summary="s", word_count=1)


# --- small builders -----------------------------------------------------------


def tool_request(*, task_id: str, payload: dict) -> ExecutionRequestMessage:
    return ExecutionRequestMessage(
        level_route=(("main_a", "main_a"), ("transform", "transform")),
        current_index=1,
        end_of_level_queue="main_a",
        task_id=task_id,
        payload=payload,
    )


def lingo_request(*, task_id: str, payload: dict) -> ExecutionRequestMessage:
    return ExecutionRequestMessage(
        level_route=(("main_a", "main_a"), ("summ", "summ")),
        current_index=1,
        end_of_level_queue="main_a",
        task_id=task_id,
        payload=payload,
    )


async def pop_result(db: AsyncBeaverDB, own_queue: str) -> dict | None:
    try:
        item = await db.queue(queue_key(own_queue)).get(block=False)
    except IndexError:
        return None
    return item.data


def slow_registry() -> AvailableToolsRegistry:
    registry = AvailableToolsRegistry()
    registry.declare(
        "slow",
        implementation="tests.test_leg082_concurrency.fake_slow_tool",
        policy={},
    )
    return registry


def make_tool_agent(
    db: AsyncBeaverDB,
    *,
    caps: ConcurrencyCaps | None = None,
    gate: ShutdownGate | None = None,
    registry: AvailableToolsRegistry | None = None,
    agent_id: str = "transform",
) -> ToolAgent:
    return ToolAgent(
        agent_id=agent_id,
        db=db,
        available_tools=registry or slow_registry(),
        tool_name="slow",
        parameters={"text": "{summ.text}"},
        input_as="summ",
        output_as="summ",
        concurrency=caps,
        drain=gate,
    )


def make_lingo_agent(
    db: AsyncBeaverDB,
    *,
    caps: ConcurrencyCaps | None = None,
    gate: ShutdownGate | None = None,
    lingo: Any,
    agent_id: str = "summ",
) -> LinguisticAgent:
    return LinguisticAgent(
        agent_id=agent_id,
        db=db,
        lingo_client=lingo,
        prompt_template="Summarize {text}.",
        output_model=SummarizeOutput,
        input_as="summ",
        output_as="summ",
        concurrency=caps,
        drain=gate,
    )


# --- per-tool cap -------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_tool_cap_limits_concurrent_executions(beaver_db: AsyncBeaverDB) -> None:
    caps = ConcurrencyCaps(tool_limits={"slow": 1})
    TOOL_GAUGE.reset(delay=0.15)
    registry = slow_registry()

    for task_id in ("T-c-1", "T-c-2"):
        await beaver_db.queue(queue_key("transform")).put(
            tool_request(task_id=task_id, payload={"summ": {"text": "a"}}).model_dump(mode="json"),
            priority=0.0,
        )

    agent_a = make_tool_agent(beaver_db, caps=caps, registry=registry)
    agent_b = make_tool_agent(beaver_db, caps=caps, registry=registry, agent_id="transform")

    outcomes = await asyncio.gather(
        agent_a.process_next(),
        agent_b.process_next(),
    )
    assert outcomes == [True, True]
    assert TOOL_GAUGE.max_seen == 1

    ids = set()
    for _ in range(2):
        item = await pop_result(beaver_db, "main_a")
        assert item is not None
        ids.add(ExecutionResultMessage.model_validate(item).task_id)
    assert ids == {"T-c-1", "T-c-2"}


@pytest.mark.asyncio
async def test_without_caps_behavior_is_unbounded(beaver_db: AsyncBeaverDB) -> None:
    TOOL_GAUGE.reset(delay=0.05)
    registry = slow_registry()

    for task_id in ("T-n-1", "T-n-2"):
        await beaver_db.queue(queue_key("transform")).put(
            tool_request(task_id=task_id, payload={"summ": {"text": "a"}}).model_dump(mode="json"),
            priority=0.0,
        )

    agent_a = make_tool_agent(beaver_db, registry=registry)
    agent_b = make_tool_agent(beaver_db, registry=registry, agent_id="transform")

    await asyncio.gather(agent_a.process_next(), agent_b.process_next())

    assert TOOL_GAUGE.max_seen == 2


# --- per-LLM cap --------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_llm_cap_limits_concurrent_calls(beaver_db: AsyncBeaverDB) -> None:
    caps = ConcurrencyCaps(max_llm=1)
    LLM_GAUGE.reset(delay=0.15)

    for task_id in ("T-l-1", "T-l-2"):
        await beaver_db.queue(queue_key("summ")).put(
            lingo_request(task_id=task_id, payload={"summ": {"text": "x"}}).model_dump(mode="json"),
            priority=0.0,
        )

    agent_a = make_lingo_agent(beaver_db, caps=caps, lingo=BlockingLLM())
    agent_b = make_lingo_agent(beaver_db, caps=caps, lingo=BlockingLLM())

    outcomes = await asyncio.gather(agent_a.process_next(), agent_b.process_next())
    assert outcomes == [True, True]
    assert LLM_GAUGE.max_seen == 1

    ids = set()
    for _ in range(2):
        item = await pop_result(beaver_db, "main_a")
        assert item is not None
        ids.add(ExecutionResultMessage.model_validate(item).task_id)
    assert ids == {"T-l-1", "T-l-2"}


# --- drain --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_stops_pulling_but_inflight_deposits(beaver_db: AsyncBeaverDB) -> None:
    gate = ShutdownGate()
    TOOL_GAUGE.reset(delay=0.5)

    for task_id in ("T-d-1", "T-d-2"):
        await beaver_db.queue(queue_key("transform")).put(
            tool_request(task_id=task_id, payload={"summ": {"text": "a"}}).model_dump(mode="json"),
            priority=0.0,
        )

    agent = make_tool_agent(beaver_db, gate=gate)
    run_task = asyncio.create_task(agent.run())

    await asyncio.wait_for(asyncio.to_thread(TOOL_GAUGE.entered.wait), timeout=2.0)
    assert not gate.draining
    gate.request()
    assert gate.draining
    steps = await asyncio.wait_for(run_task, timeout=5.0)

    assert steps == 1  # only the in-flight item was handled
    item = await pop_result(beaver_db, "main_a")
    assert item is not None
    assert ExecutionResultMessage.model_validate(item).task_id == "T-d-1"
    # T-d-2 was never pulled: the gate holds dispatches but never dequeues.
    assert await beaver_db.queue(queue_key("transform")).peek() is not None


@pytest.mark.asyncio
async def test_sigterm_sets_gate_and_drains_inflight(beaver_db: AsyncBeaverDB) -> None:
    gate = ShutdownGate()
    TOOL_GAUGE.reset(delay=0.5)
    loop = asyncio.get_running_loop()
    try:
        install_signal_drain(gate, loop=loop, signals=(signal.SIGTERM,))
    except NotImplementedError:
        pytest.skip("signal handlers unavailable on this runner")

    await beaver_db.queue(queue_key("transform")).put(
        tool_request(task_id="T-sig", payload={"summ": {"text": "a"}}).model_dump(mode="json"),
        priority=0.0,
    )
    agent = make_tool_agent(beaver_db, gate=gate)
    run_task = asyncio.create_task(agent.run())

    await asyncio.wait_for(asyncio.to_thread(TOOL_GAUGE.entered.wait), timeout=2.0)
    os.kill(os.getpid(), signal.SIGTERM)

    steps = await asyncio.wait_for(run_task, timeout=5.0)
    try:
        assert gate.draining is True
        assert steps == 1
        item = await pop_result(beaver_db, "main_a")
        assert item is not None
        assert ExecutionResultMessage.model_validate(item).task_id == "T-sig"
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


# --- invalid caps / config ----------------------------------------------------


def test_invalid_caps_rejected() -> None:
    with pytest.raises(ValueError):
        ConcurrencyCaps(tool_limits={"slow": 0})
    with pytest.raises(ValueError):
        ConcurrencyCaps(max_llm=0)


def test_tool_policy_concurrency_zero_is_loud(tmp_path) -> None:
    tools = tmp_path / "tools.yaml"
    tools.write_text(
        "available_tools:\n"
        "  slow:\n"
        "    implementation: tests.test_tools.fake_transform\n"
        "    policy:\n"
        "      concurrency: 0\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_tools_file(tools)


def test_tool_policy_concurrency_positive_loads(tmp_path) -> None:
    tools = tmp_path / "tools.yaml"
    tools.write_text(
        "available_tools:\n"
        "  slow:\n"
        "    implementation: tests.test_tools.fake_transform\n"
        "    policy:\n"
        "      concurrency: 2\n",
        encoding="utf-8",
    )
    loaded = load_tools_file(tools)
    assert loaded.available_tools["slow"].policy.concurrency == 2


def test_llm_max_concurrency_zero_is_loud(tmp_path) -> None:
    cfg = tmp_path / "legio.yaml"
    cfg.write_text(
        "services:\n"
        "  llm:\n"
        "    base_url: http://local\n"
        "    model: m\n"
        "    max_concurrency: 0\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load(config_path=cfg)


def test_llm_max_concurrency_positive_loads(tmp_path) -> None:
    cfg = tmp_path / "legio.yaml"
    cfg.write_text(
        "services:\n"
        "  llm:\n"
        "    base_url: http://local\n"
        "    model: m\n"
        "    max_concurrency: 3\n",
        encoding="utf-8",
    )
    loaded = load(config_path=cfg)
    assert loaded.config.services.llm.max_concurrency == 3


# --- materializer wiring (behavioral) -------------------------------------------

TOOL_PATTERN = """\
name: transform
type: atomic
kind: tool
input:
  input_as: summ
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
output:
  output_as: summ
  output_type: json
  output_schema:
    type: object
    properties:
      transformed: {type: string}
tool: slow
parameters:
  text: "{summ.text}"
"""

LINGO_PATTERN = """\
name: summ
type: atomic
kind: linguistic
input:
  input_as: payload
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
output:
  output_as: summ
  output_type: json
  output_schema:
    type: object
    properties:
      title: {type: string}
      summary: {type: string}
      word_count: {type: integer}
prompt: "Summarize {text}."
"""


def tools_registry_with_cap(cap: int) -> AvailableToolsRegistry:
    registry = AvailableToolsRegistry()
    registry.declare(
        "slow",
        implementation="tests.test_leg082_concurrency.fake_slow_tool",
        policy={"concurrency": cap},
    )
    return registry


@pytest.mark.asyncio
async def test_materializer_wires_tool_cap(beaver_db: AsyncBeaverDB) -> None:
    catalog = load_patterns(TOOL_PATTERN)
    TOOL_GAUGE.reset(delay=0.1)

    agents = materialize_agents(
        catalog,
        db=beaver_db,
        available_tools=tools_registry_with_cap(2),
    )
    agent = agents["transform"]
    assert isinstance(agent, ToolAgent)

    for task_id in ("T-m-1", "T-m-2", "T-m-3"):
        await beaver_db.queue(queue_key("transform")).put(
            tool_request(task_id=task_id, payload={"summ": {"text": "a"}}).model_dump(mode="json"),
            priority=0.0,
        )

    await asyncio.gather(*[agent.process_next() for _ in range(3)])
    assert TOOL_GAUGE.max_seen == 2


@pytest.mark.asyncio
async def test_materializer_wires_llm_cap(beaver_db: AsyncBeaverDB) -> None:
    catalog = load_patterns(LINGO_PATTERN)
    LLM_GAUGE.reset(delay=0.1)
    from legio.config import LlmConfig

    agents = materialize_agents(
        catalog,
        db=beaver_db,
        available_tools=AvailableToolsRegistry(),
        llm_config=LlmConfig(base_url="http://local", model="m", max_concurrency=2),
        lingo_factory=lambda llm, api_key: BlockingLLM(),
    )
    agent = agents["summ"]
    assert isinstance(agent, LinguisticAgent)

    for task_id in ("T-ml-1", "T-ml-2", "T-ml-3"):
        await beaver_db.queue(queue_key("summ")).put(
            lingo_request(task_id=task_id, payload={"payload": {"text": "x"}}).model_dump(mode="json"),
            priority=0.0,
        )

    await asyncio.gather(*[agent.process_next() for _ in range(3)])
    assert LLM_GAUGE.max_seen == 2