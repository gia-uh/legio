"""Contract tests for LEG-081 — runtime configuration loading.

The node is configured by two independent files and an environment:
- `legio.yaml` — the general node configuration (the agreed schema: node,
  database, patterns, services llm/embedding, api.clients, logging, tools
  pointer, federation known peers). Validated through typed pydantic models.
- `tools.yaml` — the independent Schema 3 config (LEG-013:
  `available_tools: {<name>: {implementation, policy}}`), validated with its
  own schema.
- `LEGIO_*` environment variables — secrets only, never YAML (LEG-017 §2),
  never logged (rule 9).

Precedence (highest wins): built-in defaults < config file (argument /
`LEGIO_CONFIG` env / default `./legio.yaml`) < CLI overrides. Missing or
invalid values are never silent: a `ConfigError` (unrecoverable, fail-fast)
surfaces the broken section.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from legio.config import (
    CliOverrides,
    ConfigError,
    LifecycleParams,
    load,
    load_tools_file,
)
from legio.errors import UnrecoverableError
from legio.patterns.schema1 import AgentKind


def write_config(tmp_path: Path, data: dict) -> Path:
    import yaml

    path = tmp_path / "legio.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def full_config_data() -> dict:
    return {
        "node": {"id": "prod-a@host-01"},
        "database": {"db_path": "./data/legio.db"},
        "patterns": {
            "tool": "./patterns/tool/",
            "linguistic": "./patterns/linguistic/",
            "composite": "./patterns/composite/",
        },
        "pools": {
            "per_pattern": {"classifier": 6, "transcriber": 4},
            "per_kind": {"tool": 4, "linguistic": 1},
            "default": 2,
        },
        "services": {
            "llm": {
                "base_url": "http://127.0.0.1:1234/v1/",
                "model": "qwen/qwen3-4b-2507",
            },
            "embedding": {
                "base_url": "http://127.0.0.1:1234/v1/",
                "model": "text-embedding-3-small",
            },
        },
        "api": {
            "host": "0.0.0.0",
            "port": 8000,
            "clients": {
                "consumer-a": {"agents": ["flow_a", "flow_b"]},
                "consumer-b": {},
            },
        },
        "logging": {"level": "INFO", "file": "./data/legio.log"},
        "lifecycle": {
            "per_pattern": {"classifier": {"drain_timeout": 120.0}},
            "per_kind": {"tool": {"drain_interval": 0.1}},
            "default": {"drain_timeout": 60.0, "drain_interval": 0.02},
        },
        "tools": {"config": "./tools.yaml"},
        "federation": {
            "peers": [
                {"id": "prod-b@host-02", "url": "http://host-02:8000"},
                {"id": "prod-c@host-03", "url": "http://host-03:8000"},
                {"id": "prod-d@host-04", "url": "http://host-04:8000"},
            ]
        },
    }


class TestDefaults:
    def test_defaults_when_no_config_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        loaded = load(env={})
        cfg = loaded.config
        assert re.match(r"^[^@]+@[^@]+$", cfg.node.id)
        assert cfg.database.db_path == Path("legio.db")
        assert cfg.patterns.tool == Path("./patterns/tool")
        assert cfg.patterns.linguistic == Path("./patterns/linguistic")
        assert cfg.patterns.composite == Path("./patterns/composite")
        assert cfg.pools.per_pattern == {}
        assert cfg.pools.per_kind == {}
        assert cfg.pools.default is None
        assert cfg.services.llm is None
        assert cfg.services.embedding is None
        assert cfg.api.host == "0.0.0.0"
        assert cfg.api.port == 8000
        assert cfg.api.clients == {}
        assert cfg.logging.level == "INFO"
        assert cfg.logging.file is None
        assert cfg.lifecycle.per_pattern == {}
        assert cfg.lifecycle.per_kind == {}
        assert cfg.lifecycle.default is None
        params = cfg.lifecycle.resolve("any-class", per_kind=None)
        assert params.drain_timeout == 300.0
        assert params.drain_interval == 0.05
        assert cfg.tools.config == Path("./tools.yaml")
        assert cfg.federation.peers == []
        assert loaded.config_path is None

    def test_logging_file_optional_defaults_to_none(self, tmp_path):
        path = write_config(tmp_path, full_config_data() | {"logging": {"level": "DEBUG"}})
        loaded = load(config_path=path, env={})
        assert loaded.config.logging.file is None
        assert loaded.config.logging.level == "DEBUG"


class TestFileLoading:
    def test_config_file_values_are_loaded(self, tmp_path):
        path = write_config(tmp_path, full_config_data())
        loaded = load(config_path=path, env={})
        cfg = loaded.config
        assert cfg.node.id == "prod-a@host-01"
        assert cfg.database.db_path == Path("./data/legio.db")
        assert cfg.patterns.tool == Path("./patterns/tool/")
        assert cfg.patterns.linguistic == Path("./patterns/linguistic/")
        assert cfg.patterns.composite == Path("./patterns/composite/")
        assert cfg.pools.per_pattern == {"classifier": 6, "transcriber": 4}
        assert set(cfg.pools.per_kind) == {AgentKind.TOOL, AgentKind.LINGUISTIC}
        assert cfg.pools.per_kind[AgentKind.TOOL] == 4
        assert cfg.pools.per_kind[AgentKind.LINGUISTIC] == 1
        assert cfg.pools.default == 2
        assert cfg.services.llm is not None
        assert cfg.services.embedding is not None
        assert cfg.services.llm.base_url == "http://127.0.0.1:1234/v1/"
        assert cfg.services.llm.model == "qwen/qwen3-4b-2507"
        assert cfg.services.embedding.model == "text-embedding-3-small"
        assert cfg.services.embedding.max_tokens_per_batch is None
        assert cfg.api.clients["consumer-a"].agents == ["flow_a", "flow_b"]
        assert cfg.api.clients["consumer-b"].agents is None
        assert cfg.logging.file == Path("./data/legio.log")
        assert cfg.lifecycle.per_pattern["classifier"].drain_timeout == 120.0
        assert cfg.lifecycle.per_kind[AgentKind.TOOL].drain_interval == 0.1
        default = cfg.lifecycle.default
        assert default is not None
        assert default.drain_timeout == 60.0
        assert default.drain_interval == 0.02
        assert cfg.tools.config == Path("./tools.yaml")
        assert [peer.id for peer in cfg.federation.peers] == [
            "prod-b@host-02",
            "prod-c@host-03",
            "prod-d@host-04",
        ]
        assert loaded.config_path == path

    def test_embedding_max_tokens_per_batch_optional(self, tmp_path):
        data = full_config_data() | {
            "services": {
                "llm": {"base_url": "http://x", "model": "m"},
                "embedding": {"base_url": "http://x", "model": "e", "max_tokens_per_batch": 4096},
            }
        }
        path = write_config(tmp_path, data)
        loaded = load(config_path=path, env={})
        assert loaded.config.services.embedding is not None
        assert loaded.config.services.embedding.max_tokens_per_batch == 4096


class TestPrecedence:
    def test_legio_config_env_selects_the_file(self, tmp_path):
        path = write_config(tmp_path, full_config_data())
        loaded = load(env={"LEGIO_CONFIG": str(path)})
        assert loaded.config.node.id == "prod-a@host-01"
        assert loaded.config_path == path

    def test_explicit_config_path_wins_over_env(self, tmp_path):
        explicit = write_config(tmp_path, full_config_data())
        other = tmp_path / "other.yaml"
        other.write_text("node:\n  id: other@host\n", encoding="utf-8")
        loaded = load(config_path=explicit, env={"LEGIO_CONFIG": str(other)})
        assert loaded.config.node.id == "prod-a@host-01"

    def test_cli_overrides_win_over_file(self, tmp_path):
        path = write_config(tmp_path, full_config_data())
        overrides = CliOverrides(
            node="cli@host",
            db_path=Path("b.db"),
            port=9090,
            log_level="DEBUG",
        )
        loaded = load(config_path=path, overrides=overrides, env={})
        cfg = loaded.config
        assert cfg.node.id == "cli@host"
        assert cfg.database.db_path == Path("b.db")
        assert cfg.api.port == 9090
        assert cfg.logging.level == "DEBUG"
        assert cfg.api.host == "0.0.0.0"

    def test_partial_overrides_keep_file_rest(self, tmp_path):
        path = write_config(tmp_path, full_config_data())
        loaded = load(config_path=path, overrides=CliOverrides(port=9100), env={})
        assert loaded.config.api.port == 9100
        assert loaded.config.api.host == "0.0.0.0"


class TestPools:
    def test_resolution_precedence_per_pattern_wins(self, tmp_path):
        path = write_config(tmp_path, full_config_data())
        cfg = load(config_path=path, env={}).config
        assert cfg.pools.resolve("classifier", per_kind=AgentKind.TOOL) == 6
        assert cfg.pools.resolve("transcriber", per_kind=AgentKind.TOOL) == 4

    def test_resolution_falls_back_by_kind_then_default(self, tmp_path):
        path = write_config(tmp_path, full_config_data())
        cfg = load(config_path=path, env={}).config
        assert cfg.pools.resolve("unlisted_tool", per_kind=AgentKind.TOOL) == 4
        assert cfg.pools.resolve("unlisted_linguistic", per_kind=AgentKind.LINGUISTIC) == 1
        assert cfg.pools.resolve("unlisted_composite", per_kind=None) == 2

    def test_resolution_returns_none_when_nothing_matches(self, tmp_path):
        path = write_config(tmp_path, {"pools": {"per_kind": {"tool": 4}}})
        cfg = load(config_path=path, env={}).config
        assert cfg.pools.resolve("unlisted_composite", per_kind=None) is None

    def test_pool_size_zero_allowed(self, tmp_path):
        data = full_config_data() | {"pools": {"per_pattern": {"classifier": 0}}}
        path = write_config(tmp_path, data)
        cfg = load(config_path=path, env={}).config
        assert cfg.pools.resolve("classifier", per_kind=AgentKind.TOOL) == 0


class TestPoolsFailFast:
    def test_negative_pool_sizes_rejected(self, tmp_path):
        data = full_config_data() | {"pools": {"per_pattern": {"classifier": -1}}}
        path = write_config(tmp_path, data)
        with pytest.raises(ConfigError):
            load(config_path=path, env={})

    def test_negative_kind_pool_rejected(self, tmp_path):
        data = full_config_data() | {"pools": {"per_kind": {"tool": -2}}}
        path = write_config(tmp_path, data)
        with pytest.raises(ConfigError):
            load(config_path=path, env={})

    def test_negative_default_rejected(self, tmp_path):
        data = full_config_data() | {"pools": {"default": -3}}
        path = write_config(tmp_path, data)
        with pytest.raises(ConfigError):
            load(config_path=path, env={})

    def test_unknown_kind_key_rejected(self, tmp_path):
        data = full_config_data() | {"pools": {"per_kind": {"foo": 2}}}
        path = write_config(tmp_path, data)
        with pytest.raises(ConfigError):
            load(config_path=path, env={})


class TestLifecycle:
    def test_resolution_precedence_per_pattern_wins(self, tmp_path):
        path = write_config(tmp_path, full_config_data())
        cfg = load(config_path=path, env={}).config
        # classifier: per_pattern timeout 120 over per_kind(tool) interval 0.1
        params = cfg.lifecycle.resolve("classifier", per_kind=AgentKind.TOOL)
        assert params.drain_timeout == 120.0
        assert params.drain_interval == 0.1

    def test_resolution_falls_back_by_kind_then_default(self, tmp_path):
        path = write_config(tmp_path, full_config_data())
        cfg = load(config_path=path, env={}).config
        params = cfg.lifecycle.resolve("unlisted_tool", per_kind=AgentKind.TOOL)
        assert params.drain_timeout == 60.0
        assert params.drain_interval == 0.1
        params = cfg.lifecycle.resolve("unlisted_composite", per_kind=None)
        assert params.drain_timeout == 60.0
        assert params.drain_interval == 0.02

    def test_resolution_overlays_partial_levels(self, tmp_path):
        path = write_config(tmp_path, {"lifecycle": {"per_kind": {"tool": {"drain_interval": 0.5}}}})
        cfg = load(config_path=path, env={}).config
        params = cfg.lifecycle.resolve("some-tool", per_kind=AgentKind.TOOL)
        assert params.drain_timeout == 300.0
        assert params.drain_interval == 0.5

    def test_resolution_returns_builtin_when_nothing_matches(self, tmp_path):
        path = write_config(tmp_path, {"lifecycle": {"per_kind": {"tool": {}}}})
        cfg = load(config_path=path, env={}).config
        params = cfg.lifecycle.resolve("unlisted_composite", per_kind=None)
        assert params.drain_timeout == 300.0
        assert params.drain_interval == 0.05

    def test_empty_per_pattern_params_are_builtin(self):
        params = LifecycleParams()
        assert params.drain_timeout is None
        assert params.drain_interval is None

    def test_non_positive_budgets_rejected(self, tmp_path):
        data = {"lifecycle": {"default": {"drain_timeout": 0}}}
        path = write_config(tmp_path, data)
        with pytest.raises(ConfigError):
            load(config_path=path, env={})

    def test_non_positive_interval_rejected(self, tmp_path):
        data = {"lifecycle": {"per_kind": {"tool": {"drain_interval": -1}}}}
        path = write_config(tmp_path, data)
        with pytest.raises(ConfigError):
            load(config_path=path, env={})

    def test_unknown_kind_key_rejected(self, tmp_path):
        data = {"lifecycle": {"per_kind": {"foo": {"drain_timeout": 10}}}}
        path = write_config(tmp_path, data)
        with pytest.raises(ConfigError):
            load(config_path=path, env={})


class TestFailFast:
    def test_invalid_node_id_rejected(self, tmp_path):
        path = write_config(tmp_path, {"node": {"id": "bad"}})
        with pytest.raises(ConfigError) as exc:
            load(config_path=path, env={})
        assert "node" in str(exc.value).lower() or "id" in str(exc.value).lower()

    def test_invalid_peer_id_rejected(self, tmp_path):
        data = full_config_data() | {"federation": {"peers": [{"id": "nodep", "url": "http://x"}]}}
        path = write_config(tmp_path, data)
        with pytest.raises(ConfigError):
            load(config_path=path, env={})

    def test_invalid_yaml_rejected(self, tmp_path):
        path = tmp_path / "legio.yaml"
        path.write_text("node:\n  id: [unclosed\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load(config_path=path, env={})

    def test_missing_explicit_config_file_raises(self, tmp_path):
        with pytest.raises(ConfigError):
            load(config_path=tmp_path / "nope.yaml", env={})

    def test_config_error_is_unrecoverable(self):
        assert issubclass(ConfigError, UnrecoverableError)


class TestSecrets:
    def test_secrets_come_from_env_only(self, tmp_path):
        data = full_config_data() | {
            "api": {
                "host": "0.0.0.0",
                "port": 8000,
                "clients": {
                    "consumer-a": {
                        "agents": ["flow_a"],
                        "token": "ignore-me-in-yaml",
                    }
                },
            }
        }
        path = write_config(tmp_path, data)
        loaded = load(config_path=path, env={})
        assert loaded.secrets.client_tokens == {}
        assert loaded.secrets.llm_api_key is None
        dump = loaded.config.api.clients["consumer-a"].model_dump()
        assert "token" not in dump

    def test_all_secret_env_vars_are_collected(self, tmp_path):
        path = write_config(tmp_path, full_config_data())
        env = {
            "LEGIO_LLM_API_KEY": "llmkey",
            "LEGIO_EMBEDDING_API_KEY": "embkey",
            "LEGIO_FEDERATION_TOKEN": "fedtoken",
            "LEGIO_CLIENT_TOKEN_ACME": "acmetok",
            "LEGIO_CLIENT_TOKEN_OTHER": "othertok",
            "UNRELATED": "not-a-secret",
        }
        loaded = load(config_path=path, env=env)
        secrets = loaded.secrets
        assert secrets.llm_api_key == "llmkey"
        assert secrets.embedding_api_key == "embkey"
        assert secrets.federation_token == "fedtoken"
        assert secrets.client_tokens == {"ACME": "acmetok", "OTHER": "othertok"}


class TestToolsFile:
    def test_tools_file_loaded_and_validated(self, tmp_path):
        path = tmp_path / "tools.yaml"
        path.write_text(
            "available_tools:\n"
            "  simple_transcription_tool:\n"
            "    implementation: consumer.tools.simple_transcription_tool\n"
            "    policy: {timeout: 120, retries: 3}\n"
            "  summarize_flow_tool:\n"
            "    implementation: consumer.tools.summarize_flow_tool\n",
            encoding="utf-8",
        )
        tools = load_tools_file(path)
        assert set(tools.available_tools) == {
            "simple_transcription_tool",
            "summarize_flow_tool",
        }
        decl = tools.available_tools["simple_transcription_tool"]
        assert decl.implementation == "consumer.tools.simple_transcription_tool"
        assert decl.policy.timeout == 120
        assert decl.policy.retries == 3
        assert tools.available_tools["summarize_flow_tool"].policy.timeout is None

    def test_tool_missing_implementation_rejected(self, tmp_path):
        path = tmp_path / "tools.yaml"
        path.write_text("available_tools:\n  bad_tool:\n    policy: {}\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_tools_file(path)

    def test_tool_invalid_name_rejected(self, tmp_path):
        path = tmp_path / "tools.yaml"
        path.write_text(
            "available_tools:\n  Bad_Tool:\n    implementation: consumer.tools.t\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError):
            load_tools_file(path)

    def test_tool_implementation_needs_dotted_path(self, tmp_path):
        path = tmp_path / "tools.yaml"
        path.write_text(
            "available_tools:\n  plain_impl:\n    implementation: notadottedpath\n",
            encoding="utf-8",
        )
        with pytest.raises(ConfigError):
            load_tools_file(path)
