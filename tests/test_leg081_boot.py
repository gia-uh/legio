"""Contract tests for LEG-081 — Runtime: materializer + config-driven node boot.

The Runtime is the missing engine piece: given the LoadedConfig (LEG-081 config
schema, validated in ``test_leg081_config``) it connects the database, loads and
*validates* the three pattern directories first (any invalid pattern refuses the
boot before anything binds — rule 9), loads the independent Schema 3 tools file
into the tool registry, and *materializes* the standing agent map from the
validated Catalog through the injected resource seams (tool registry, lingo
client factory, concrete composite classes). Unmaterializable specs — a tool
without a declaration, a linguistic agent without an LLM service/factory, a
composite without a concrete class — fail the boot loudly, naming the agent.

The two independent config files (``legio.yaml`` + ``tools.yaml``) are loaded by
``legio.config`` (LEG-081); this module proves the boot consumes them whole.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from beaver import AsyncBeaverDB

from legio.agents import CompositeAgent, LinguisticAgent, ToolAgent
from legio.config import CliOverrides, load
from legio.errors import ConfigError, UnrecoverableError
from legio.flow import build_payload
from legio.materializer import LingoFactory, NodeRuntime, boot_node, materialize_agents
from legio.naming import result_queue_key
from legio.patterns import Catalog, load_patterns
from legio.security import ClientTokenStore
from legio.tools import AvailableToolsRegistry

SUMM_YAML = """\
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
      lang: {type: string}
output:
  output_as: summ
  output_type: json
  output_schema:
    type: object
    properties:
      title: {type: string}
      summary: {type: string}
      word_count: {type: integer}
prompt: "Summarize {text} and {lang}."
"""

ASSESS_YAML = """\
name: assess
type: atomic
kind: tool
input:
  input_as: summ
  input_type: json
  input_schema:
    type: object
    properties:
      title: {type: string}
      summary: {type: string}
output:
  output_as: result
  output_type: json
  output_schema:
    type: object
    properties:
      result: {type: string}
tool: assess
parameters:
  title: "{summ.title}"
  summary: "{summ.summary}"
"""

SUMMARIZE_YAML = """\
name: summarize
type: composite
main: true
input:
  input_as: payload
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
      lang: {type: string}
output:
  output_as: result
  output_type: json
  output_schema:
    type: object
    properties:
      result:
        type: object
        properties:
          result: {type: string}
branches:
  - - summ
    - assess
"""

LEGIO_YAML = """\
node:
  id: "boot@test"
database:
  db_path: "{db_path}"
patterns:
  tool: "{tool_dir}"
  linguistic: "{linguistic_dir}"
  composite: "{composite_dir}"
tools:
  config: "{tools_file}"
api:
  clients:
    client-a:
      agents: null
services:
  llm:
    base_url: "http://llm.test"
    model: "llm-model"
"""

TOOLS_YAML = """\
available_tools:
  assess:
    implementation: "tests.test_leg081_boot.fake_assess"
    policy:
      timeout: 30
      retries: 0
"""

ENV_TOKENS = {"LEGIO_CLIENT_TOKEN_client-a": "tok-a"}


class GatherComposite(CompositeAgent):
    """A concrete composite pattern (fictitious domain): transcribes each
    branch's built payload (slot order) into its own ``output_as``."""

    async def build_output_as(self, info):
        gathered: dict = {}

        for branch_payload in info.values():
            gathered.update(branch_payload)
        return build_payload(gathered, output_as=self._output_as)


def fake_assess(title: str, summary: str) -> dict:
    return {"result": f"[{title}] {summary}"}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class RecordLingo:
    """A test fake matching the linguistic call contract.

    ``create(model, messages)`` validates a record against the model the agent
    requests (a per-spec compiled ``output_model``) and returns it as the
    response — the same shape the real ``lingo`` client satisfies.
    """

    def __init__(self, record: dict[str, Any]) -> None:
        self._record = dict(record)

    async def create(self, model, messages) -> Any:
        return model(**self._record)


def mock_lingo_factory(**record: str | int) -> LingoFactory:
    """A lingo factory whose client answers the compiled ``output_model``."""

    def factory(config, api_key) -> RecordLingo:
        return RecordLingo(record)

    return factory


@pytest.fixture
def node_dirs(tmp_path: Path) -> dict[str, str]:
    """A node filesystem: three pattern dirs + legio.yaml + tools.yaml."""
    tool_dir = tmp_path / "patterns" / "tool"
    linguistic_dir = tmp_path / "patterns" / "linguistic"
    composite_dir = tmp_path / "patterns" / "composite"
    tool_dir.mkdir(parents=True)
    linguistic_dir.mkdir(parents=True)
    composite_dir.mkdir(parents=True)
    (tool_dir / "assess.yaml").write_text(ASSESS_YAML, encoding="utf-8")
    (linguistic_dir / "summ.yaml").write_text(SUMM_YAML, encoding="utf-8")
    (composite_dir / "summarize.yaml").write_text(SUMMARIZE_YAML, encoding="utf-8")
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
    return {"config": str(legio_yaml), "db": str(db_path)}


def unified_catalog() -> Catalog:
    """Unify the three pattern snippets into one in-memory catalog."""
    return load_patterns(SUMM_YAML + "---\n" + ASSESS_YAML + "---\n" + SUMMARIZE_YAML)


# --------------------------------------------------------------------------
# materialize_agents — the missing engine piece
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_materialize_tool_agent(beaver_db: AsyncBeaverDB) -> None:
    catalog = unified_catalog()
    registry = AvailableToolsRegistry()
    registry.declare(
        "assess",
        implementation="tests.test_leg081_boot.fake_assess",
        policy={"timeout": 30, "retries": 0},
    )

    agents = materialize_agents(
        catalog,
        db=beaver_db,
        available_tools=registry,
        lingo_factory=mock_lingo_factory(),
        composite_classes={"summarize": GatherComposite},
    )

    agent = agents["assess"]
    assert isinstance(agent, ToolAgent)
    assert agent.agent_id == "assess"
    assert agent._tool_name == "assess"
    assert agent._parameters == {"title": "{summ.title}", "summary": "{summ.summary}"}


@pytest.mark.asyncio
async def test_materialize_linguistic_agent_compiles_output_model(
    beaver_db: AsyncBeaverDB,
) -> None:
    catalog = unified_catalog()
    registry = AvailableToolsRegistry()
    registry.declare(
        "assess",
        implementation="tests.test_leg081_boot.fake_assess",
        policy={"timeout": 30, "retries": 0},
    )

    agents = materialize_agents(
        catalog,
        db=beaver_db,
        available_tools=registry,
        lingo_factory=mock_lingo_factory(),
        composite_classes={"summarize": GatherComposite},
    )

    agent = agents["summ"]
    assert isinstance(agent, LinguisticAgent)
    record = agent._output_model(title="T", summary="S", word_count=3)
    assert record.model_dump()["summary"] == "S"


@pytest.mark.asyncio
async def test_materialize_composite_resolves_branches(beaver_db: AsyncBeaverDB) -> None:
    catalog = unified_catalog()
    registry = AvailableToolsRegistry()
    registry.declare(
        "assess",
        implementation="tests.test_leg081_boot.fake_assess",
        policy={"timeout": 30, "retries": 0},
    )

    agents = materialize_agents(
        catalog,
        db=beaver_db,
        available_tools=registry,
        lingo_factory=mock_lingo_factory(),
        composite_classes={"summarize": GatherComposite},
    )

    agent = agents["summarize"]
    assert isinstance(agent, GatherComposite)
    assert agent._branches == [(("summ", "payload"), ("assess", "summ"))]


@pytest.mark.asyncio
async def test_materialize_builds_atomics_before_composites(beaver_db: AsyncBeaverDB) -> None:
    """DAG order: atomic agents are materialized before the composites."""
    catalog = unified_catalog()
    registry = AvailableToolsRegistry()
    registry.declare(
        "assess",
        implementation="tests.test_leg081_boot.fake_assess",
        policy={"timeout": 30, "retries": 0},
    )

    order: list[str] = []
    agents = materialize_agents(
        catalog,
        db=beaver_db,
        available_tools=registry,
        lingo_factory=mock_lingo_factory(),
        composite_classes={"summarize": GatherComposite},
        on_built=lambda name: order.append(name),
    )
    assert list(agents) == ["summ", "assess", "summarize"]
    assert order.index("summ") < order.index("summarize")
    assert order.index("assess") < order.index("summarize")


@pytest.mark.asyncio
async def test_undeclared_tool_fails_boot_naming_agent(beaver_db: AsyncBeaverDB) -> None:
    catalog = unified_catalog()
    registry = AvailableToolsRegistry()  # assess never declared

    with pytest.raises(UnrecoverableError, match="assess"):
        materialize_agents(
            catalog,
            db=beaver_db,
            available_tools=registry,
            lingo_factory=mock_lingo_factory(),
            composite_classes={"summarize": GatherComposite},
        )


@pytest.mark.asyncio
async def test_linguistic_without_llm_service_fails_naming_agent(
    beaver_db: AsyncBeaverDB,
) -> None:
    catalog = unified_catalog()
    registry = AvailableToolsRegistry()
    registry.declare(
        "assess",
        implementation="tests.test_leg081_boot.fake_assess",
        policy={"timeout": 30, "retries": 0},
    )

    with pytest.raises(UnrecoverableError, match="summ"):
        materialize_agents(
            catalog,
            db=beaver_db,
            available_tools=registry,
            lingo_factory=None,  # services.llm absent and no injected factory
            composite_classes={"summarize": GatherComposite},
        )


@pytest.mark.asyncio
async def test_composite_without_concrete_class_fails_naming_composite(
    beaver_db: AsyncBeaverDB,
) -> None:
    catalog = unified_catalog()
    registry = AvailableToolsRegistry()
    registry.declare(
        "assess",
        implementation="tests.test_leg081_boot.fake_assess",
        policy={"timeout": 30, "retries": 0},
    )

    with pytest.raises(UnrecoverableError, match="summarize"):
        materialize_agents(
            catalog,
            db=beaver_db,
            available_tools=registry,
            lingo_factory=mock_lingo_factory(),
            composite_classes=None,
        )


# --------------------------------------------------------------------------
# boot_node — config → connect → load → validate → materialize → app
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_loads_three_pattern_dirs_and_materializes(
    beaver_db: AsyncBeaverDB, node_dirs: dict[str, str]
) -> None:
    loaded = load(
        node_dirs["config"],
        env=ENV_TOKENS,
    )
    runtime = await boot_node(
        loaded,
        db=beaver_db,
        lingo_factory=mock_lingo_factory(),
        composite_classes={"summarize": GatherComposite},
    )

    assert isinstance(runtime, NodeRuntime)
    assert runtime.config is loaded
    assert set(runtime.agents) == {"summ", "assess", "summarize"}
    assert isinstance(runtime.agents["summ"], LinguisticAgent)
    assert isinstance(runtime.agents["assess"], ToolAgent)
    assert isinstance(runtime.agents["summarize"], GatherComposite)
    assert runtime.starting_agents == frozenset({"summarize"})


@pytest.mark.asyncio
async def test_boot_materializes_app_and_client_store(
    beaver_db: AsyncBeaverDB, node_dirs: dict[str, str]
) -> None:
    loaded = load(node_dirs["config"], env=ENV_TOKENS)
    runtime = await boot_node(
        loaded,
        db=beaver_db,
        lingo_factory=mock_lingo_factory(),
        composite_classes={"summarize": GatherComposite},
    )

    assert isinstance(runtime.client_store, ClientTokenStore)
    assert runtime.client_store.resolve_consumer_id("tok-a") == "client-a"
    app = runtime.app
    assert app.title == "legio"


@pytest.mark.asyncio
async def test_boot_client_without_token_logs_warning_and_not_registered(
    beaver_db: AsyncBeaverDB,
    node_dirs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    loaded = load(node_dirs["config"], env={})  # no LEGIO_CLIENT_TOKEN_*
    with caplog.at_level(logging.WARNING, logger="legio.materializer"):
        runtime = await boot_node(
            loaded,
            db=beaver_db,
            lingo_factory=mock_lingo_factory(),
            composite_classes={"summarize": GatherComposite},
        )

    assert isinstance(runtime.client_store, ClientTokenStore)
    assert runtime.client_store.resolve_consumer_id("tok-a") is None
    assert any("client-a" in message for message in caplog.messages)


@pytest.mark.asyncio
async def test_boot_missing_pattern_dir_fails_loudly(
    beaver_db: AsyncBeaverDB, node_dirs: dict[str, str], tmp_path: Path
) -> None:
    missing = tmp_path / "does" / "not" / "exist"
    overrides = CliOverrides(tool_dir=missing)
    loaded = load(node_dirs["config"], overrides=overrides, env=ENV_TOKENS)

    with pytest.raises(UnrecoverableError, match="tool"):
        await boot_node(
            loaded,
            db=beaver_db,
            lingo_factory=mock_lingo_factory(),
            composite_classes={"summarize": GatherComposite},
        )


@pytest.mark.asyncio
async def test_boot_invalid_pattern_refuses_before_binding(
    beaver_db: AsyncBeaverDB,
    node_dirs: dict[str, str],
    tmp_path: Path,
) -> None:
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "broken.yaml").write_text(
        SUMMARIZE_YAML.replace("name: summarize", "name: broken").replace(
            "branches:\n  - - summ\n    - assess",
            "branches:\n  - - summ\n    - validator",
        ),
        encoding="utf-8",
    )
    overrides = CliOverrides(composite_dir=bad)
    loaded = load(node_dirs["config"], overrides=overrides, env=ENV_TOKENS)

    with pytest.raises(UnrecoverableError, match="unknown"):
        await boot_node(
            loaded,
            db=beaver_db,
            lingo_factory=mock_lingo_factory(),
            composite_classes={"summarize": GatherComposite},
        )


@pytest.mark.asyncio
async def test_boot_missing_tools_file_fails_loudly(
    beaver_db: AsyncBeaverDB,
    node_dirs: dict[str, str],
    tmp_path: Path,
) -> None:
    missing = tmp_path / "no-tools.yaml"
    overrides = CliOverrides(tools_config=missing)
    loaded = load(node_dirs["config"], overrides=overrides, env=ENV_TOKENS)

    with pytest.raises(ConfigError, match="cannot read"):
        await boot_node(
            loaded,
            db=beaver_db,
            lingo_factory=mock_lingo_factory(),
            composite_classes={"summarize": GatherComposite},
        )


@pytest.mark.asyncio
async def test_boot_default_llm_factory_builds_real_lingo_client(
    beaver_db: AsyncBeaverDB, node_dirs: dict[str, str]
) -> None:
    """Without an injected factory, legio builds lingo.LLM from services.llm."""
    env = {"LEGIO_LLM_API_KEY": "test-key", **ENV_TOKENS}
    loaded = load(node_dirs["config"], env=env)
    runtime = await boot_node(
        loaded,
        db=beaver_db,
        composite_classes={"summarize": GatherComposite},
    )

    summ = runtime.agents["summ"]
    assert isinstance(summ, LinguisticAgent)
    assert summ._lingo.model == "llm-model"
    assert str(summ._lingo.client.base_url).rstrip("/") == "http://llm.test"


@pytest.mark.asyncio
async def test_boot_full_flow_over_rest_uses_configured_node_id(
    beaver_db: AsyncBeaverDB, node_dirs: dict[str, str]
) -> None:
    loaded = load(node_dirs["config"], env=ENV_TOKENS)
    runtime = await boot_node(
        loaded,
        db=beaver_db,
        lingo_factory=mock_lingo_factory(title="Foxes", summary="A note.", word_count=4),
        composite_classes={"summarize": GatherComposite},
    )
    app = runtime.app
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/submit",
            json={"agent": "summarize", "payload": {"text": "The quick brown fox.", "lang": "en"}},
            headers=bearer("tok-a"),
        )
        assert resp.status_code == 200, resp.text
        task_id = resp.json()["task_id"]
        assert task_id.startswith("boot@test:")

        steps = await runtime.supervise()

        status_resp = await ac.get(f"/status/{task_id}", headers=bearer("tok-a"))
        assert status_resp.status_code == 200, status_resp.text
        entry = status_resp.json()
        assert entry["state"] == "completed"
        assert entry["result_key"] == result_queue_key(task_id)
        assert entry["output"]["result"]["result"]["result"] == "[Foxes] A note."
        assert steps > 0


# --------------------------------------------------------------------------
# Supervise — the node's single polling loop
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supervise_idle_boot_returns_zero(
    beaver_db: AsyncBeaverDB, node_dirs: dict[str, str]
) -> None:
    """A freshly booted node with no submissions is idle: supervise does nothing."""
    loaded = load(node_dirs["config"], env=ENV_TOKENS)
    runtime = await boot_node(
        loaded,
        db=beaver_db,
        lingo_factory=mock_lingo_factory(),
        composite_classes={"summarize": GatherComposite},
    )

    assert await runtime.supervise() == 0
    assert await runtime.supervise() == 0


@pytest.mark.asyncio
async def test_supervise_drains_concurrent_submits_to_idle(
    beaver_db: AsyncBeaverDB, node_dirs: dict[str, str]
) -> None:
    """Two concurrent submissions are both delivered: the supervisor polls every
    materialized agent (the non-'main' capability agents too), moving each flow
    to its final-result queue and then idling."""
    loaded = load(node_dirs["config"], env=ENV_TOKENS)
    runtime = await boot_node(
        loaded,
        db=beaver_db,
        lingo_factory=mock_lingo_factory(title="Foxes", summary="A note.", word_count=4),
        composite_classes={"summarize": GatherComposite},
    )
    app = runtime.app
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        task_ids = []
        for text, lang in (
            ("The quick brown fox.", "en"),
            ("El rápido zorro marrón.", "es"),
        ):
            resp = await ac.post(
                "/submit",
                json={"agent": "summarize", "payload": {"text": text, "lang": lang}},
                headers=bearer("tok-a"),
            )
            assert resp.status_code == 200, resp.text
            task_ids.append(resp.json()["task_id"])

        steps = await runtime.supervise()
        assert await runtime.supervise() == 0

        for task_id in task_ids:
            status_resp = await ac.get(f"/status/{task_id}", headers=bearer("tok-a"))
            assert status_resp.status_code == 200, status_resp.text
            assert status_resp.json()["state"] == "completed"
            output = status_resp.json()["output"]["result"]["result"]
            assert output == {"result": "[Foxes] A note."}
        assert steps >= 2