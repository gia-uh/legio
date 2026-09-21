"""Contract tests for LEG-092 — work-item over HTTP + remote deposit.

``POST /work-items/{agent}`` with the shared federation token (L1, LEG-017) and
a matching interface deposits into the acceptor's queue. The author has already
resolved the step against this acceptor's roster (LEG-091); the acceptor
double-checks the interface and deposits a genuine business task keyed by the
author's task id (idempotent: a duplicate is never executed twice — LEG-093
prerequisite).

Local-first framing (ARCH §9, maintainer session 85q): federation transports
*wok*, never lifecycle. The surface only deposits work items; there is no
lifecycle verb over it (a node controls only its own agents).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from beaver import AsyncBeaverDB

from legio.config import load
from legio.flow import SCHEMA_VERSION
from legio.materializer import boot_node
from legio.patterns import Catalog, load_patterns
from legio.runtime import Runtime

logger = logging.getLogger("legio.tests.leg092")

NODE_ID = "recipient@test"
AUTHOR_NODE = "author@test"

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


def capacity_catalog() -> Catalog:
    """The acceptor's served capacity: a single tool agent ``cutter``."""
    return load_patterns(TOOL_YAML)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def build_app(
    beaver_db: AsyncBeaverDB,
    catalog: Catalog | None = None,
    *,
    federation_token: str | None = "fed-secret",
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


def work_item(
    *,
    task_id: str = f"{AUTHOR_NODE}:11111111-1111-1111-1111-111111111111",
    payload: dict[str, Any] | None = None,
    schema_version: int = SCHEMA_VERSION,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "payload": payload or {"raw": "carve"},
        "schema_version": schema_version,
    }


# --------------------------------------------------------------------------
# § a valid work item deposits into the acceptor's class queue (task id from
#   the author), and is executed once
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_work_item_deposits_into_acceptor_queue(beaver_db: AsyncBeaverDB) -> None:
    app = build_app(beaver_db, capacity_catalog())
    runtime = Runtime(beaver_db, node_id=NODE_ID)
    async with await _client(app) as ac:
        item = work_item()
        resp = await ac.post("/work-items/cutter", json=item, headers=bearer("fed-secret"))
        assert resp.status_code == 200, resp.text
        receipt = resp.json()
        assert receipt["deposited"] is True
        assert receipt["deduplicated"] is False
        assert receipt["id"] == item["task_id"]

        # Deposit is a genuine Manager task on the acceptor runtime, keyed by
        # the author's task id: unwinding it reaches the class queue as a root
        # ExecutionRequestMessage whose payload is re-keyed under input_as.
        record = await runtime.manager.status(item["task_id"])
        assert record is not None
        assert record.kwargs["token"]["task_id"] == item["task_id"]


@pytest.mark.asyncio
async def test_same_work_item_is_never_deposited_twice(beaver_db: AsyncBeaverDB) -> None:
    app = build_app(beaver_db, capacity_catalog())
    runtime = Runtime(beaver_db, node_id=NODE_ID)
    async with await _client(app) as ac:
        item = work_item()
        first = await ac.post("/work-items/cutter", json=item, headers=bearer("fed-secret"))
        assert first.status_code == 200
        assert first.json()["deposited"] is True

        duplicate = await ac.post("/work-items/cutter", json=item, headers=bearer("fed-secret"))
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json()["deposited"] is False
        assert duplicate.json()["deduplicated"] is True

        # Exactly one task record exists on the acceptor for the author's id.
        count = 0
        async for key in runtime.manager._tasks.keys():
            record = await runtime.manager.status(key)
            if record is not None and record.task_id == item["task_id"]:
                count += 1
        assert count == 1


# --------------------------------------------------------------------------
# § L1 guard: no token → 401, wrong token → 401, unconfigured → 404
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_work_item_requires_federation_token(beaver_db: AsyncBeaverDB) -> None:
    app = build_app(beaver_db, capacity_catalog())
    async with await _client(app) as ac:
        missing = await ac.post("/work-items/cutter", json=work_item())
        assert missing.status_code == 401
        assert missing.json()["code"] == "unauthorized"

        wrong = await ac.post(
            "/work-items/cutter", json=work_item(), headers=bearer("not-the-secret")
        )
        assert wrong.status_code == 401


@pytest.mark.asyncio
async def test_work_item_absent_without_federation_config(beaver_db: AsyncBeaverDB) -> None:
    app = build_app(beaver_db, capacity_catalog(), federation_token=None)
    async with await _client(app) as ac:
        resp = await ac.post("/work-items/cutter", json=work_item())
        assert resp.status_code == 404


# --------------------------------------------------------------------------
# § acceptor-side interface/scope validation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_work_item_to_unknown_agent_is_404(beaver_db: AsyncBeaverDB) -> None:
    app = build_app(beaver_db, capacity_catalog())
    async with await _client(app) as ac:
        resp = await ac.post("/work-items/ghost", json=work_item(), headers=bearer("fed-secret"))
        assert resp.status_code == 404
        assert resp.json()["code"] == "unknown_agent"


@pytest.mark.asyncio
async def test_work_item_with_mismatched_schema_version_is_409(
    beaver_db: AsyncBeaverDB,
) -> None:
    app = build_app(beaver_db, capacity_catalog())
    async with await _client(app) as ac:
        resp = await ac.post(
            "/work-items/cutter",
            json=work_item(schema_version=999),
            headers=bearer("fed-secret"),
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "interface_mismatch"
        # nothing deposited
        runtime = Runtime(beaver_db, node_id=NODE_ID)
        assert (
            await runtime.manager.status(f"{AUTHOR_NODE}:11111111-1111-1111-1111-111111111111")
            is None
        )


# --------------------------------------------------------------------------
# § lifecycle stays local: the surface never accepts an operator verb
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_work_item_surface_never_accepts_lifecycle_verbs(
    beaver_db: AsyncBeaverDB,
) -> None:
    app = build_app(beaver_db, capacity_catalog())
    async with await _client(app) as ac:
        verb = work_item()
        verb["verb"] = "destroy"
        resp = await ac.post("/work-items/cutter", json=verb, headers=bearer("fed-secret"))
        # The request model forbids extra fields — a lifecycle verb is simply
        # not part of the work-item contract (it is refused, never dropped).
        assert resp.status_code in (400, 422)


# --------------------------------------------------------------------------
# § the booted node accepts work items over REST when the L1 token is set
# --------------------------------------------------------------------------


LEGIO_YAML = """\
node:
  id: "recipient@test"
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
    implementation: "tests.test_leg092_work_item.fake_cutter"
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


@pytest.fixture
def node_dirs(tmp_path: Path) -> dict[str, str]:
    tool_dir = tmp_path / "patterns" / "tool"
    linguistic_dir = tmp_path / "patterns" / "linguistic"
    composite_dir = tmp_path / "patterns" / "composite"
    tool_dir.mkdir(parents=True)
    linguistic_dir.mkdir(parents=True)
    composite_dir.mkdir(parents=True)
    (tool_dir / "cutter.yaml").write_text(TOOL_YAML, encoding="utf-8")
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
async def test_booted_node_accepts_work_item_over_rest(
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
        composite_classes={},
    )
    transport = httpx.ASGITransport(app=booted.app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        item = work_item()
        resp = await ac.post(
            "/work-items/cutter",
            json=item,
            headers=bearer("fed-secret"),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["deposited"] is True

        record = await booted.runtime.manager.status(item["task_id"])
        assert record is not None
        assert record.kwargs["token"]["launcher_class"] == "cutter"

        denied = await ac.post("/work-items/cutter", json=work_item())
        assert denied.status_code == 401
