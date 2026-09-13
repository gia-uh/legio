"""Contract tests for LEG-090 — per-node catalog (roster from capacity).

The node's executable capacity is its served pattern catalog (LEG-021/070);
``GET /catalog`` derives the roster from ``pattern_catalog.served()`` and guards
it with the shared federation token (L1, LEG-017) — no token → 401, wrong token
→ 401, no federation configured → the endpoint is 404 (no surface).

The endpoint is authoritative for LEG-091/092: a peer trusts this roster to
author remote work with a matching ``schema_version`` (the flow/messages
version the node speaks).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from beaver import AsyncBeaverDB

from legio.agents import CompositeAgent
from legio.config import load
from legio.flow import SCHEMA_VERSION
from legio.materializer import boot_node
from legio.patterns import Catalog, load_patterns
from legio.runtime import Runtime

logger = logging.getLogger("legio.tests.leg090")

NODE_ID = "leg090@test"

TOOL_YAML = """\
name: cutter
type: atomic
kind: tool
input:
  input_as: payload
  input_type: json
  input_schema:
    type: object
    properties:
      raw: {type: string}
output:
  output_as: result
  output_type: json
  output_schema:
    type: object
    properties:
      result: {type: string}
tool: cutter
parameters:
  raw: "{payload.raw}"
"""

LING_YAML = """\
name: admirer
type: atomic
kind: linguistic
input:
  input_as: payload
  input_type: json
  input_schema:
    type: object
    properties:
      raw: {type: string}
output:
  output_as: result
  output_type: json
  output_schema:
    type: object
    properties:
      result: {type: string}
prompt: "Admire {raw}."
"""

FLOW_YAML = """\
name: carve
type: composite
main: true
input:
  input_as: payload
  input_type: json
  input_schema:
    type: object
    properties:
      raw: {type: string}
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
  - - cutter
  - - admirer
"""


def capacity_catalog() -> Catalog:
    """A served catalog: one tool, one linguistic, one composite."""
    return load_patterns(TOOL_YAML + "---\n" + LING_YAML + "---\n" + FLOW_YAML)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def build_app(
    beaver_db: AsyncBeaverDB,
    catalog: Catalog | None = None,
    *,
    federation_token: str | None,
):
    from legio.api import create_app

    runtime = Runtime(beaver_db, node_id=NODE_ID)
    return create_app(
        runtime=runtime,
        pattern_catalog=catalog,
        federation_token=federation_token,
    )


async def _client(app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# --------------------------------------------------------------------------
# § capacity derivation from the served catalog
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_catalog_lists_served_capacity_with_interface_and_kind(
    beaver_db: AsyncBeaverDB,
) -> None:
    app = build_app(beaver_db, capacity_catalog(), federation_token="fed-secret")
    async with await _client(app) as ac:
        resp = await ac.get("/catalog", headers=bearer("fed-secret"))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["schema_version"] == SCHEMA_VERSION
        by_name = {entry["agent"]: entry for entry in body["agents"]}

        cutter = by_name["cutter"]
        assert cutter["kind"] == "tool"
        assert cutter["interface"]["capability"] == "cutter"
        assert cutter["interface"]["schema_version"] == SCHEMA_VERSION

        admirer = by_name["admirer"]
        assert admirer["kind"] == "linguistic"
        assert admirer["interface"]["capability"] == "admirer"
        assert admirer["interface"]["schema_version"] == SCHEMA_VERSION

        carve = by_name["carve"]
        assert carve["kind"] == "composite"
        assert carve["interface"]["capability"] == "carve"
        assert carve["interface"]["schema_version"] == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_catalog_tracks_invalidation(beaver_db: AsyncBeaverDB) -> None:
    catalog = capacity_catalog()
    catalog.invalidate("cutter")
    app = build_app(beaver_db, catalog, federation_token="fed-secret")
    async with await _client(app) as ac:
        resp = await ac.get("/catalog", headers=bearer("fed-secret"))
        assert resp.status_code == 200, resp.text
        names = {entry["agent"] for entry in resp.json()["agents"]}
        assert "cutter" not in names
        assert "carve" not in names
        assert "admirer" in names


# --------------------------------------------------------------------------
# § L1 guard: no token → 401, wrong token → 401, unconfigured → 404
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_catalog_requires_federation_token(beaver_db: AsyncBeaverDB) -> None:
    app = build_app(beaver_db, capacity_catalog(), federation_token="fed-secret")
    async with await _client(app) as ac:
        missing = await ac.get("/catalog")
        assert missing.status_code == 401
        assert missing.json()["code"] == "unauthorized"

        wrong = await ac.get("/catalog", headers=bearer("not-the-secret"))
        assert wrong.status_code == 401


@pytest.mark.asyncio
async def test_catalog_absent_without_federation_config(beaver_db: AsyncBeaverDB) -> None:
    app = build_app(beaver_db, capacity_catalog(), federation_token=None)
    async with await _client(app) as ac:
        resp = await ac.get("/catalog")
        assert resp.status_code == 404


# --------------------------------------------------------------------------
# § the booted node serves the catalog when the L1 token is configured
# --------------------------------------------------------------------------


LEGIO_YAML = """\
node:
  id: "leg090@test"
database:
  db_path: "{db_path}"
patterns:
  tool: "{tool_dir}"
  linguistic: "{linguistic_dir}"
  composite: "{composite_dir}"
tools:
  config: "{tools_file}"
"""

TOOLS_YAML = """\
available_tools:
  cutter:
    implementation: "tests.test_leg090_catalog.fake_cutter"
    policy:
      timeout: 30
      retries: 0
"""


def fake_cutter(raw: str | None) -> dict[str, str]:
    return {"result": f"cut {raw}"}


class RecordLingo:
    """Lingo fake answering the compiled ``output_model`` (proven harness)."""

    def __init__(self, record: dict[str, Any]) -> None:
        self._record = dict(record)

    async def create(self, model, messages) -> Any:
        return model(**self._record)


def mock_lingo_factory(**record: str | int):
    def factory(config, api_key) -> RecordLingo:
        return RecordLingo(record)

    return factory


class CarveComposite(CompositeAgent):
    """Concrete composite: concatenate each branch's built payload."""

    async def build_output_as(self, info):
        gathered: dict = {}
        for branch_payload in info.values():
            gathered.update(branch_payload)
        return {"result": gathered}


@pytest.fixture
def node_dirs(tmp_path: Path) -> dict[str, str]:
    tool_dir = tmp_path / "patterns" / "tool"
    linguistic_dir = tmp_path / "patterns" / "linguistic"
    composite_dir = tmp_path / "patterns" / "composite"
    tool_dir.mkdir(parents=True)
    linguistic_dir.mkdir(parents=True)
    composite_dir.mkdir(parents=True)
    (tool_dir / "cutter.yaml").write_text(TOOL_YAML, encoding="utf-8")
    (linguistic_dir / "admirer.yaml").write_text(LING_YAML, encoding="utf-8")
    (composite_dir / "carve.yaml").write_text(FLOW_YAML, encoding="utf-8")
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


@pytest.mark.asyncio
async def test_booted_node_serves_catalog_over_rest(
    beaver_db: AsyncBeaverDB, node_dirs: dict[str, str]
) -> None:
    loaded = load(
        node_dirs["config"],
        env={"LEGIO_FEDERATION_TOKEN": "fed-secret"},
    )
    booted = await boot_node(
        loaded,
        db=beaver_db,
        lingo_factory=mock_lingo_factory(),
        composite_classes={"carve": CarveComposite},
    )
    transport = httpx.ASGITransport(app=booted.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        ok = await ac.get("/catalog", headers=bearer("fed-secret"))
        assert ok.status_code == 200, ok.text
        names = {entry["agent"] for entry in ok.json()["agents"]}
        assert names == {"cutter", "admirer", "carve"}
        assert ok.json()["schema_version"] == SCHEMA_VERSION

        denied = await ac.get("/catalog")
        assert denied.status_code == 401