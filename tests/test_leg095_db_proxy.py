"""Contract tests for LEG-095 Phase 3 — node-injected db proxy + federated deposit.

Cross-node queue deposits with no direct remote write ever: agents keep one
``db`` handle (a transparent ``AsyncBeaverDB`` proxy — ``NodeDB``), internal
names hit local beaver, foreign names deposit onto the owning node's
federation-only ``POST /deposits``, and the owner performs the local ``put``.
Reads on foreign names are a visible violation (LEG-095 Phase 3 contract).
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

import httpx
import pytest
import respx
from beaver import AsyncBeaverDB

from legio.agents.tool_agent import ToolAgent
from legio.api import (
    CatalogAgentEntry,
    CatalogAgentInterface,
    CatalogResponse,
    create_app,
)
from legio.errors import RecoverableError
from legio.federation import NodeDB, build_routes, fetch_peer_catalogs
from legio.flow import ExecutionRequestMessage
from legio.naming import queue_key
from legio.patterns import load_patterns
from legio.runtime import Runtime
from legio.tools import AvailableToolsRegistry

logger = logging.getLogger("legio.tests.leg095proxy")

NODE_A = "node-a@test"
NODE_B = "node-b@test"
PEER_B_URL = "http://peer-b"
PEER_A_URL = "http://peer-a"
TOKEN = "fed-secret"

TOOL_YAML = """\
name: {name}
type: atomic
kind: tool
input:
  input_as: {name}
  input_type: json
  input_schema:
    type: object
    properties:
      text: {{type: string}}
output:
  output_as: {name}
  output_type: json
  output_schema:
    type: object
    properties:
      text: {{type: string}}
tool: {name}
parameters:
  text: "{{{name}.text}}"
"""


def fake_text(text: str) -> dict:
    """Domain-free fake tool: echoes under a new key shape."""
    return {"text": str(text).upper()}


def tool_catalog(name: str):
    return load_patterns(TOOL_YAML.format(name=name))


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def task_id(node: str) -> str:
    return f"{node}:{uuid.uuid4()}"


def proxy_for(
    db: AsyncBeaverDB,
    node_id: str,
    peer_url: str,
    peer_id: str,
    *,
    routes: dict[str, str] | None = None,
    token: str = TOKEN,
    app=None,
) -> NodeDB:
    """A NodeDB whose remote leg talks to ``app`` (ASGI, no network)."""
    client = (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=peer_url)
        if app is not None
        else None
    )
    peers = {peer_id: peer_url}
    return NodeDB(
        db,
        node_id=node_id,
        routes=routes or {},
        peers=peers,
        federation_token=token,
        client=client,
    )


def build_owner_app(beaver_db: AsyncBeaverDB, node_id: str, agent: str):
    """The peer owner's app: serves ``agent`` over the federation surface."""
    runtime = Runtime(beaver_db, node_id=node_id)
    return create_app(
        runtime=runtime,
        pattern_catalog=tool_catalog(agent),
        federation_token=TOKEN,
    )


def build_tool_agent(
    db: AsyncBeaverDB, name: str, tool_impl: str = "tests.test_leg095_db_proxy.fake_text"
) -> ToolAgent:
    registry = AvailableToolsRegistry()
    registry.declare(name, implementation=tool_impl, policy={"timeout": 30, "retries": 0})
    return ToolAgent(
        agent_id=name,
        db=db,
        available_tools=registry,
        tool_name=name,
        parameters={"text": f"{{{name}.text}}"},
        input_as=name,
        output_as=name,
    )


# --------------------------------------------------------------------------
# § build_routes: local-first, first-peer-wins
# --------------------------------------------------------------------------


def test_build_routes_prefers_local_and_first_peer() -> None:
    routes = build_routes(
        {"home"},
        {"peer-b@test": ["away", "home"], "peer-c@test": ["away", "far"]},
    )
    assert routes == {"away": "peer-b@test", "far": "peer-c@test"}


def test_build_routes_empty_without_peers() -> None:
    assert build_routes({"home"}, {}) == {}
    assert build_routes(set(), {}) == {}


# --------------------------------------------------------------------------
# § proxy transparency: no peers/routes behaves like raw beaver
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_without_routes_is_transparent(beaver_db: AsyncBeaverDB) -> None:
    from beaver import AsyncBeaverDB as BeaverDB

    db = NodeDB(beaver_db, node_id=NODE_A)
    assert isinstance(db, BeaverDB)
    await db.queue(queue_key("cutter")).put({"n": 1}, priority=0.0)
    item = await beaver_db.queue(queue_key("cutter")).get(block=False)
    assert item.data == {"n": 1}
    assert await db.queue(queue_key("cutter")).count() == 0
    assert await db.queue(queue_key("cutter")).peek() is None
    d = db.dict("gates")
    await d.set("cutter", {"state": "enabled"})
    assert await beaver_db.dict("gates").fetch("cutter") == {"state": "enabled"}


# --------------------------------------------------------------------------
# § remote agent deposit travels via POST /deposits, nothing lands locally
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_agent_deposit_reaches_owner_queue(
    beaver_db: AsyncBeaverDB,
) -> None:
    import tempfile

    from beaver import AsyncBeaverDB as BeaverDB

    owner_dir = tempfile.mkdtemp()
    owner_db = BeaverDB(f"{owner_dir}/owner.db")
    await owner_db.connect()
    try:
        owner_app = build_owner_app(owner_db, NODE_B, "cutter")
        db = proxy_for(
            beaver_db, NODE_A, PEER_B_URL, NODE_B, routes={"cutter": NODE_B}, app=owner_app
        )
        await db.queue(queue_key("cutter")).put({"task_id": "T-1"}, priority=0.0)
        item = await owner_db.queue(queue_key("cutter")).get(block=False)
        assert item.data == {"task_id": "T-1"}
        assert await beaver_db.queue(queue_key("cutter")).count() == 0
    finally:
        await owner_db.close()


@pytest.mark.asyncio
async def test_gather_queue_routes_via_its_agent(beaver_db: AsyncBeaverDB) -> None:
    import tempfile

    from beaver import AsyncBeaverDB as BeaverDB

    owner_dir = tempfile.mkdtemp()
    owner_db = BeaverDB(f"{owner_dir}/owner.db")
    await owner_db.connect()
    try:
        owner_app = build_owner_app(owner_db, NODE_B, "comp")
        db = proxy_for(
            beaver_db, NODE_A, PEER_B_URL, NODE_B, routes={"comp": NODE_B}, app=owner_app
        )
        await db.queue(queue_key("gather:comp")).put({"branch": 1}, priority=0.0)
        item = await owner_db.queue(queue_key("gather:comp")).get(block=False)
        assert item.data == {"branch": 1}
    finally:
        await owner_db.close()


# --------------------------------------------------------------------------
# § result queues follow the task origin
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_queue_follows_task_origin(beaver_db: AsyncBeaverDB) -> None:
    import tempfile

    from beaver import AsyncBeaverDB as BeaverDB

    owner_dir = tempfile.mkdtemp()
    owner_db = BeaverDB(f"{owner_dir}/owner.db")
    await owner_db.connect()
    try:
        owner_app = build_owner_app(owner_db, NODE_B, "cutter")
        db = proxy_for(beaver_db, NODE_A, PEER_B_URL, NODE_B, app=owner_app)
        foreign = task_id(NODE_B)
        await db.queue(queue_key(f"result:{foreign}")).put({"done": True}, priority=0.0)
        item = await owner_db.queue(queue_key(f"result:{foreign}")).get(block=False)
        assert item.data == {"done": True}

        local = task_id(NODE_A)
        await db.queue(queue_key(f"result:{local}")).put({"done": True}, priority=0.0)
        item = await beaver_db.queue(queue_key(f"result:{local}")).get(block=False)
        assert item.data == {"done": True}

        # Malformed (legacy/test shapes) stay local — back-compat, never an error.
        await db.queue(queue_key("result:T-legacy")).put({"done": True}, priority=0.0)
        item = await beaver_db.queue(queue_key("result:T-legacy")).get(block=False)
        assert item.data == {"done": True}
    finally:
        await owner_db.close()


@pytest.mark.asyncio
async def test_result_queue_unknown_origin_fails_visibly(
    beaver_db: AsyncBeaverDB,
) -> None:
    db = proxy_for(beaver_db, NODE_A, PEER_B_URL, NODE_B, app=None)
    ghost = f"ghost@test:{uuid.uuid4()}"
    with pytest.raises(RecoverableError):
        await db.queue(queue_key(f"result:{ghost}")).put({"done": True}, priority=0.0)


# --------------------------------------------------------------------------
# § foreign reads are a visible violation; remote failures are visible
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_foreign_queue_reads_raise_visibly(beaver_db: AsyncBeaverDB) -> None:
    db = proxy_for(beaver_db, NODE_A, PEER_B_URL, NODE_B, routes={"cutter": NODE_B}, app=None)
    foreign = db.queue(queue_key("cutter"))
    with pytest.raises(RecoverableError):
        await foreign.get(block=False)
    with pytest.raises(RecoverableError):
        await foreign.peek()
    with pytest.raises(RecoverableError):
        await foreign.count()


@pytest.mark.asyncio
async def test_owner_refusal_raises_visibly_and_deposits_nothing(
    beaver_db: AsyncBeaverDB,
) -> None:
    import tempfile

    from beaver import AsyncBeaverDB as BeaverDB

    owner_dir = tempfile.mkdtemp()
    owner_db = BeaverDB(f"{owner_dir}/owner.db")
    await owner_db.connect()
    try:
        owner_app = build_owner_app(owner_db, NODE_B, "cutter")
        await owner_db.dict("gates").set("cutter", {"state": "disabled"})
        db = proxy_for(
            beaver_db, NODE_A, PEER_B_URL, NODE_B, routes={"cutter": NODE_B}, app=owner_app
        )
        with pytest.raises(RecoverableError, match="class_disabled"):
            await db.queue(queue_key("cutter")).put({"task_id": "T-9"}, priority=0.0)
        assert await owner_db.queue(queue_key("cutter")).count() == 0
        assert await beaver_db.queue(queue_key("cutter")).count() == 0
    finally:
        await owner_db.close()


@pytest.mark.asyncio
@respx.mock
async def test_unreachable_owner_raises_visibly(beaver_db: AsyncBeaverDB) -> None:
    respx.post(f"{PEER_B_URL}/deposits").mock(
        side_effect=httpx.ConnectError("peer down", request=None)
    )
    async with httpx.AsyncClient() as client:
        db = NodeDB(
            beaver_db,
            node_id=NODE_A,
            routes={"cutter": NODE_B},
            peers={NODE_B: PEER_B_URL},
            federation_token=TOKEN,
            client=client,
        )
        with pytest.raises(RecoverableError, match="node-b"):
            await db.queue(queue_key("cutter")).put({"task_id": "T-9"}, priority=0.0)


# --------------------------------------------------------------------------
# § POST /deposits pins (validation order, codes)
# --------------------------------------------------------------------------


def deposit_app(beaver_db: AsyncBeaverDB, agent: str, *, token: str | None = TOKEN):
    runtime = Runtime(beaver_db, node_id=NODE_B)
    return create_app(runtime=runtime, pattern_catalog=tool_catalog(agent), federation_token=token)


async def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_deposits_rejects_unauthorized(beaver_db: AsyncBeaverDB) -> None:
    app = deposit_app(beaver_db, "cutter")
    async with await _client(app) as ac:
        body = {"queue": queue_key("cutter"), "item": {"a": 1}, "priority": 0.0}
        assert (await ac.post("/deposits", json=body)).status_code == 401
        assert (await ac.post("/deposits", json=body, headers=bearer("wrong"))).status_code == 401


@pytest.mark.asyncio
async def test_deposits_absent_without_federation(beaver_db: AsyncBeaverDB) -> None:
    app = deposit_app(beaver_db, "cutter", token=None)
    async with await _client(app) as ac:
        body = {"queue": queue_key("cutter"), "item": {"a": 1}, "priority": 0.0}
        assert (await ac.post("/deposits", json=body, headers=bearer(TOKEN))).status_code == 404


@pytest.mark.asyncio
async def test_deposits_no_capacity_without_catalog(beaver_db: AsyncBeaverDB) -> None:
    runtime = Runtime(beaver_db, node_id=NODE_B)
    app = create_app(runtime=runtime, pattern_catalog=None, federation_token=TOKEN)
    async with await _client(app) as ac:
        body = {"queue": queue_key("cutter"), "item": {"a": 1}, "priority": 0.0}
        resp = await ac.post("/deposits", json=body, headers=bearer(TOKEN))
        assert resp.status_code == 503
        assert resp.json()["code"] == "no_capacity"


@pytest.mark.asyncio
async def test_deposits_rejects_bad_shape_and_unknown_agent(
    beaver_db: AsyncBeaverDB,
) -> None:
    app = deposit_app(beaver_db, "cutter")
    async with await _client(app) as ac:
        bad = await ac.post(
            "/deposits",
            json={"queue": "nope", "item": {"a": 1}},
            headers=bearer(TOKEN),
        )
        assert bad.status_code == 422
        unknown = await ac.post(
            "/deposits",
            json={"queue": queue_key("ghost"), "item": {"a": 1}},
            headers=bearer(TOKEN),
        )
        assert unknown.status_code == 404
        assert unknown.json()["code"] == "unknown_agent"
        junk = await ac.post(
            "/deposits",
            json={"queue": queue_key("cutter"), "item": {"a": 1}, "smuggle": True},
            headers=bearer(TOKEN),
        )
        assert junk.status_code == 422


@pytest.mark.asyncio
async def test_deposits_rejects_disabled_class(beaver_db: AsyncBeaverDB) -> None:
    app = deposit_app(beaver_db, "cutter")
    await beaver_db.dict("gates").set("cutter", {"state": "disabled"})
    async with await _client(app) as ac:
        resp = await ac.post(
            "/deposits",
            json={"queue": queue_key("cutter"), "item": {"a": 1}},
            headers=bearer(TOKEN),
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "class_disabled"
        assert await beaver_db.queue(queue_key("cutter")).count() == 0


@pytest.mark.asyncio
async def test_deposits_lands_agent_gather_and_result(beaver_db: AsyncBeaverDB) -> None:
    app = deposit_app(beaver_db, "cutter")
    tid = task_id(NODE_A)
    async with await _client(app) as ac:
        for queue in (
            queue_key("cutter"),
            queue_key("gather:cutter"),
            queue_key(f"result:{tid}"),
        ):
            resp = await ac.post(
                "/deposits",
                json={"queue": queue, "item": {"task_id": tid}, "priority": 0.0},
                headers=bearer(TOKEN),
            )
            assert resp.status_code == 200, resp.text
            assert resp.json() == {"queue": queue, "deposited": True}
    for queue in (
        queue_key("cutter"),
        queue_key("gather:cutter"),
        queue_key(f"result:{tid}"),
    ):
        assert await beaver_db.queue(queue).count() == 1


# --------------------------------------------------------------------------
# § fetch_peer_catalogs helper (L1, fail-fast)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_fetch_peer_catalogs_parses_rosters() -> None:
    respx.get(f"{PEER_B_URL}/catalog").mock(
        return_value=httpx.Response(
            200,
            json={
                "schema_version": 1000,
                "agents": [
                    {
                        "agent": "cutter",
                        "interface": {"capability": "cutter", "schema_version": 1000},
                        "kind": "tool",
                        "input_as": "cutter_in",
                    }
                ],
            },
        )
    )
    rosters = await fetch_peer_catalogs({NODE_B: PEER_B_URL}, TOKEN)
    assert set(rosters) == {NODE_B}
    assert [entry.agent for entry in rosters[NODE_B].agents] == ["cutter"]
    assert rosters[NODE_B].agents[0].input_as == "cutter_in"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_peer_catalogs_failure_is_visible() -> None:
    respx.get(f"{PEER_B_URL}/catalog").mock(
        side_effect=httpx.ConnectError("peer down", request=None)
    )
    with pytest.raises(RecoverableError, match="peer-b"):
        await fetch_peer_catalogs({NODE_B: PEER_B_URL}, TOKEN)


# --------------------------------------------------------------------------
# § cross-node drills through real agent code (no far-side vehicle)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forward_deposit_travels_through_agent_advance(
    beaver_db: AsyncBeaverDB,
) -> None:
    """A's ToolAgent advances a 2-step route whose second step lives only on
    B: the advance deposit crosses via the proxy (zero flow changes)."""
    import tempfile

    from beaver import AsyncBeaverDB as BeaverDB

    owner_dir = tempfile.mkdtemp()
    owner_db = BeaverDB(f"{owner_dir}/owner.db")
    await owner_db.connect()
    try:
        owner_app = build_owner_app(owner_db, NODE_B, "xray")
        adb = proxy_for(
            beaver_db, NODE_A, PEER_B_URL, NODE_B, routes={"xray": NODE_B}, app=owner_app
        )
        agent = build_tool_agent(adb, "tango")
        tid = task_id(NODE_A)
        request = ExecutionRequestMessage(
            level_route=(("tango", "tango"), ("xray", "xray")),
            current_index=0,
            end_of_level_queue=queue_key(f"result:{tid}"),
            level=1,
            launcher_class="tango",
            task_id=tid,
            branch_id="branch-drill",
            payload={"tango": {"text": "hi"}},
        )
        await adb.queue(queue_key("tango")).put(request.model_dump(mode="json"), priority=0.0)
        assert await agent.run() == 1
        item = await owner_db.queue(queue_key("xray")).get(block=False)
        assert item.data["task_id"] == tid
        assert item.data["current_index"] == 1
        assert await beaver_db.queue(queue_key("xray")).count() == 0
    finally:
        await owner_db.close()


@pytest.mark.asyncio
async def test_return_deposit_travels_through_agent_advance(
    beaver_db: AsyncBeaverDB,
) -> None:
    """Mirror: B's ToolAgent advances toward a step served only on A; the
    deposit (and a result-bound one) land on A's queues."""
    import tempfile

    from beaver import AsyncBeaverDB as BeaverDB

    home_dir = tempfile.mkdtemp()
    home_db = BeaverDB(f"{home_dir}/home.db")
    await home_db.connect()
    try:
        home_app = build_owner_app(home_db, NODE_A, "yankee")
        bdb = proxy_for(
            beaver_db, NODE_B, PEER_A_URL, NODE_A, routes={"yankee": NODE_A}, app=home_app
        )
        agent = build_tool_agent(bdb, "zulu")
        tid = task_id(NODE_A)
        request = ExecutionRequestMessage(
            level_route=(("zulu", "zulu"), ("yankee", "yankee")),
            current_index=0,
            end_of_level_queue=queue_key(f"result:{tid}"),
            level=1,
            launcher_class="zulu",
            task_id=tid,
            branch_id="branch-drill-back",
            payload={"zulu": {"text": "hi"}},
        )
        await bdb.queue(queue_key("zulu")).put(request.model_dump(mode="json"), priority=0.0)
        assert await agent.run() == 1
        item = await home_db.queue(queue_key("yankee")).get(block=False)
        assert item.data["task_id"] == tid
    finally:
        await home_db.close()


# --------------------------------------------------------------------------
# § boot composition: agents materialize over the proxy with routes
# --------------------------------------------------------------------------

BOOT_TOOL_YAML = """\
name: cutter
type: atomic
kind: tool
main: true
input:
  input_as: cutter
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
output:
  output_as: cutter
  output_type: json
  output_schema:
    type: object
    properties:
      text: {type: string}
tool: cutter
parameters:
  text: "{cutter.text}"
"""

BOOT_TOOLS_YAML = """\
available_tools:
  cutter:
    implementation: "tests.test_leg095_db_proxy.fake_text"
    policy:
      timeout: 30
      retries: 0
"""

BOOT_LEGIO_YAML = """\
node:
  id: "node-a@test"
database:
  db_path: "{db_path}"
patterns:
  tool: "{tool_dir}"
  linguistic: "{linguistic_dir}"
  composite: "{composite_dir}"
tools:
  config: "{tools_file}"
federation:
  peers:
    - id: "node-b@test"
      url: "http://peer-b"
"""

BOOT_LEGIO_YAML_NO_PEERS = """\
node:
  id: "node-a@test"
database:
  db_path: "{db_path}"
patterns:
  tool: "{tool_dir}"
  linguistic: "{linguistic_dir}"
  composite: "{composite_dir}"
tools:
  config: "{tools_file}"
"""


def _boot_dirs(tmp_path: Path, *, peers: bool) -> dict[str, str]:
    tool_dir = tmp_path / "patterns" / "tool"
    linguistic_dir = tmp_path / "patterns" / "linguistic"
    composite_dir = tmp_path / "patterns" / "composite"
    tool_dir.mkdir(parents=True)
    linguistic_dir.mkdir(parents=True)
    composite_dir.mkdir(parents=True)
    (tool_dir / "cutter.yaml").write_text(BOOT_TOOL_YAML, encoding="utf-8")
    tools_file = tmp_path / "tools.yaml"
    tools_file.write_text(BOOT_TOOLS_YAML, encoding="utf-8")
    db_path = tmp_path / "node.db"
    legio_yaml = tmp_path / "legio.yaml"
    template = BOOT_LEGIO_YAML if peers else BOOT_LEGIO_YAML_NO_PEERS
    legio_yaml.write_text(
        template.format(
            db_path=db_path,
            tool_dir=tool_dir,
            linguistic_dir=linguistic_dir,
            composite_dir=composite_dir,
            tools_file=tools_file,
        ),
        encoding="utf-8",
    )
    return {"config": str(legio_yaml), "db": str(db_path)}


def _peer_roster(agent: str) -> CatalogResponse:
    return CatalogResponse(
        schema_version=1000,
        agents=[
            CatalogAgentEntry(
                agent=agent,
                interface=CatalogAgentInterface(capability=agent, schema_version=1000),
                kind="tool",
                input_as=f"{agent}_in",
            )
        ],
    )


@pytest.mark.asyncio
async def test_boot_wraps_agents_in_proxy_with_routes(
    tmp_path: Path, beaver_db: AsyncBeaverDB
) -> None:
    from legio.config import load
    from legio.materializer import boot_node

    dirs = _boot_dirs(tmp_path, peers=True)
    loaded = load(dirs["config"], env={"LEGIO_FEDERATION_TOKEN": TOKEN})
    booted = await boot_node(loaded, db=beaver_db, peer_catalogs={NODE_B: _peer_roster("xray")})
    agent_db = booted.agents["cutter"]._db
    assert isinstance(agent_db, NodeDB)
    assert agent_db.routes == {"xray": NODE_B}
    assert booted.db is beaver_db


@pytest.mark.asyncio
async def test_boot_without_peers_is_pure_local_proxy(
    tmp_path: Path, beaver_db: AsyncBeaverDB
) -> None:
    from legio.config import load
    from legio.materializer import boot_node

    dirs = _boot_dirs(tmp_path, peers=False)
    loaded = load(dirs["config"], env={"LEGIO_FEDERATION_TOKEN": TOKEN})
    booted = await boot_node(loaded, db=beaver_db)
    agent_db = booted.agents["cutter"]._db
    assert isinstance(agent_db, NodeDB)
    assert agent_db.routes == {}
    await agent_db.queue(queue_key("cutter")).put({"n": 1}, priority=0.0)
    item = await beaver_db.queue(queue_key("cutter")).get(block=False)
    assert item.data == {"n": 1}
