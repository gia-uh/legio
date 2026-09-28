"""LEG-115 — the node config is strict: an unknown key fails the load.

pydantic's default ``extra="ignore"`` silently dropped a typo or a misplaced
secret (``services.llm.api_key``, ``api.clients.<n>.token``). The node config and
the tools file now forbid extras, loudly (rule 9).
"""

from __future__ import annotations

import pathlib

import pytest

from legio.config import ConfigError, load, load_tools_file


def _write(tmp_path: pathlib.Path, data: str) -> pathlib.Path:
    path = tmp_path / "legio.yaml"
    path.write_text(data, encoding="utf-8")
    return path


BASE = (
    'node:\n  id: "x@y"\n'
    'database:\n  db_path: "x.db"\n'
    'patterns:\n  tool: "./patterns/tool"\n'
    "  linguistic: ./patterns/linguistic\n"
    "  composite: ./patterns/composite\n"
)


def test_unknown_top_level_key_fails_load(tmp_path: pathlib.Path) -> None:
    path = _write(tmp_path, BASE + "databse:\n  db_path: typo.db\n")
    with pytest.raises(ConfigError, match="databse"):
        load(config_path=path, env={})


def test_unknown_nested_key_fails_load(tmp_path: pathlib.Path) -> None:
    # A secret misplaced in YAML (secrets are env-only) is now a hard error.
    path = _write(
        tmp_path,
        BASE
        + 'services:\n  llm:\n    base_url: "http://x/v1"\n'
        + '    model: "m"\n    api_key: "in-yaml"\n',
    )
    with pytest.raises(ConfigError, match="api_key"):
        load(config_path=path, env={})


def test_unknown_key_under_a_client_fails_load(tmp_path: pathlib.Path) -> None:
    path = _write(tmp_path, BASE + "api:\n  clients:\n    demo:\n      token: nope\n")
    with pytest.raises(ConfigError, match="token"):
        load(config_path=path, env={})


def test_tools_file_unknown_key_fails(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "tools.yaml"
    path.write_text(
        "available_tools:\n"
        "  t:\n"
        "    implementation: tests.test_tools.fake_transform\n"
        "    retries: 2\n",  # belongs under `policy`, not here
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="retries"):
        load_tools_file(path)


def test_shipped_example_configs_are_still_valid() -> None:
    """No shipped example relied on a silently ignored key."""
    root = pathlib.Path(__file__).resolve().parents[1] / "examples"
    for node in (
        "transform",
        "summarize",
        "extract-and-summarize",
        "distribute-summary",
        "document_processing",
    ):
        loaded = load(root / node / "legio.yaml", env={})
        assert loaded.config.node.id.endswith("@example")
