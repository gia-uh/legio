"""Contract tests for LEG-094 — 3-node directed example (domain-free, in-repo).

The R-9 federation validation on the **directed model** (85w/85y, LEG-094 spec
session 86e): three symmetric nodes share one federation token (L1); A's
``audit`` flow fans out a 2-step branch ``[spot, refine]`` — ``spot`` runs on C,
``refine`` on B, and the branch result returns to A's ``gather:audit`` over the
same message-carried stamps (level_route / current_index / end_of_level_queue /
branch_id / task_id). Symmetric: B's ``brief`` fans out ``[spot, utter]`` — C
then A. Federation only transports work: each cross-node hop is a token-guarded
``POST /deposits`` recorded on the in-process mesh; the author's own node is the
only one that drains the accepted final result.

The example also proves the three designed gap closures (spec §A—§C): the
roster advertises each entry's ``input_as`` (live ``fetch_peer_catalogs`` parse);
branch steps naming peer-offered capabilities load and resolve on the booted
node (local catalog ∪ peer rosters); and composite dependencies are recorded as
the **local** steps, so a branches-only-remote composite is born enabled.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from beaver import AsyncBeaverDB

from legio.agents import CompositeAgent
from legio.config import load
from legio.federation import PeerCatalogEntry, PeerRoster, fetch_peer_catalogs
from legio.materializer import boot_node
from legio.registry import ActivityState
from legio.runtime import TaskState

NODE_A = "node-a@test"
NODE_B = "node-b@test"
NODE_C = "node-c@test"
HOST_A = "http://node-a.test"
HOST_B = "http://node-b.test"
HOST_C = "http://node-c.test"
TOKEN = "fed-secret"

UTTER_AS = "utter_in"
AUDIT_AS = "audit_in"
REFINE_AS = "refine_in"
BRIEF_AS = "brief_in"
SPOT_AS = "spot_in"


# --- domain-free patterns (rule 7): one tool, two linguistic leaves, two flows -


UTTER_YAML = """\
name: utter
type: atomic
kind: tool
input:
  input_as: {utter_as}
  input_type: json
  input_schema:
    type: object
    properties:
      raw: {{type: string}}
output:
  output_as: text
  output_type: json
  output_schema:
    type: object
    properties:
      raw: {{type: string}}
tool: utter
parameters:
  raw: "{{{utter_as}.raw}}"
"""

SPOT_YAML = """\
name: spot
type: atomic
kind: linguistic
input:
  input_as: {spot_as}
  input_type: json
  input_schema:
    type: object
    properties:
      raw: {{type: string}}
output:
  output_as: spot
  output_type: json
  output_schema:
    type: object
    properties:
      raw: {{type: string}}
prompt: "Spot {{raw}}."
"""

REFINE_YAML = """\
name: refine
type: atomic
kind: linguistic
input:
  input_as: {refine_as}
  input_type: json
  input_schema:
    type: object
    properties:
      raw: {{type: string}}
output:
  output_as: refined
  output_type: json
  output_schema:
    type: object
    properties:
      raw: {{type: string}}
prompt: "Refine {{raw}}."
"""

AUDIT_YAML = """\
name: audit
type: composite
main: true
input:
  input_as: {audit_as}
  input_type: json
  input_schema:
    type: object
    properties:
      raw: {{type: string}}
output:
  output_as: result
  output_type: json
  output_schema:
    type: object
    properties:
      raw: {{type: string}}
branches:
  - - spot
    - refine
"""

BRIEF_YAML = """\
name: brief
type: composite
main: true
input:
  input_as: {brief_as}
  input_type: json
  input_schema:
    type: object
    properties:
      raw: {{type: string}}
output:
  output_as: result
  output_type: json
  output_schema:
    type: object
    properties:
      raw: {{type: string}}
branches:
  - - spot
    - utter
"""


def shout(raw: str) -> dict:
    """Domain-free fake tool: the author node's own capacity."""
    return {"raw": str(raw).upper() + "!"}


class RecordLingo:
    """Lingo fake answering the compiled ``output_model`` (proven harness)."""

    def __init__(self, record: dict) -> None:
        self._record = dict(record)

    async def create(self, model, messages):
        return model(**self._record)


def lingo_factory(record: dict):
    def factory(config, api_key) -> RecordLingo:
        return RecordLingo(record)

    return factory


class MergeComposite(CompositeAgent):
    """Concrete composite: fold the branch's single leaf under ``result``."""

    async def build_output_as(self, info):
        slot = next(iter(info.values()))
        leaf = next(iter(slot.values()))
        return {"result": {"raw": leaf["raw"]}}


# --- the in-process mesh: route by hostname to the three booted apps ----------


class MeshTransport(httpx.AsyncBaseTransport):
    """A host-routing ASGI mesh; records every ``/deposits`` wire request."""

    def __init__(self) -> None:
        self.apps: dict[str, object] = {}
        self.deposits: list[tuple[str, str, str, str | None]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        app = self.apps[request.url.host]
        if request.url.path == "/deposits":
            import json

            body = json.loads(request.content.decode("utf-8"))
            self.deposits.append(
                (
                    request.url.host,
                    body.get("queue", ""),
                    str(request.url),
                    request.headers.get("authorization"),
                )
            )
        inner = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
        return await inner.handle_async_request(request)


def clear_wire(mesh: MeshTransport) -> None:
    mesh.deposits.clear()


def foreign_deposits(mesh: MeshTransport) -> list[tuple[str, str]]:
    return [(host, queue) for host, queue, _, _ in mesh.deposits]


def assert_deposits_wire(mesh: MeshTransport, expected: set[tuple[str, str]]) -> None:
    """Exactly the expected foreign deposits crossed the mesh, token-guarded."""
    actual = set(foreign_deposits(mesh))
    assert actual == expected, f"unexpected wire: {actual - expected}"
    for _, _, _, auth in mesh.deposits:
        assert auth == f"Bearer {TOKEN}"


# --- node filesystems: one pattern/tool directory tree per node --------------


def _write_empty_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _node_dirs(tmp_path: Path, name: str, *, peers: list[tuple[str, str]]) -> dict[str, str]:
    """A node's filesystem: its own patterns + tools + legio.yaml + peers."""
    base = tmp_path / f"node-{name}"
    tool_dir = base / "patterns" / "tool"
    ling_dir = base / "patterns" / "linguistic"
    comp_dir = base / "patterns" / "composite"
    _write_empty_dir(tool_dir)
    _write_empty_dir(ling_dir)
    _write_empty_dir(comp_dir)
    if name == "a":
        (tool_dir / "utter.yaml").write_text(
            UTTER_YAML.format(utter_as=UTTER_AS), encoding="utf-8"
        )
        (comp_dir / "audit.yaml").write_text(
            AUDIT_YAML.format(audit_as=AUDIT_AS), encoding="utf-8"
        )
    elif name == "b":
        (ling_dir / "refine.yaml").write_text(
            REFINE_YAML.format(refine_as=REFINE_AS), encoding="utf-8"
        )
        (comp_dir / "brief.yaml").write_text(
            BRIEF_YAML.format(brief_as=BRIEF_AS), encoding="utf-8"
        )
    else:
        (ling_dir / "spot.yaml").write_text(
            SPOT_YAML.format(spot_as=SPOT_AS), encoding="utf-8"
        )
    tools_path = base / "tools.yaml"
    if name == "a":
        tools_path.write_text(
            "available_tools:\n"
            "  utter:\n"
            '    implementation: "tests.test_leg094_multi_node_example.shout"\n'
            "    policy:\n"
            "      timeout: 30\n"
            "      retries: 0\n",
            encoding="utf-8",
        )
    else:
        tools_path.write_text("available_tools: {}\n", encoding="utf-8")
    db_path = base / "node.db"
    legio_yaml = base / "legio.yaml"
    peer_lines = "\n".join(
        f'    - id: "{peer_id}"\n      url: "{peer_url}"' for peer_id, peer_url in peers
    )
    node_id = f"node-{name}@test"
    legio_yaml.write_text(
        f"""\
node:
  id: "{node_id}"
database:
  db_path: "{db_path}"
patterns:
  tool: "{tool_dir}"
  linguistic: "{ling_dir}"
  composite: "{comp_dir}"
tools:
  config: "{tools_path}"
federation:
  peers:
{peer_lines}
""",
        encoding="utf-8",
    )
    return {"config": str(legio_yaml), "db": str(db_path)}


def _roster(entries: dict[str, str]) -> PeerRoster:
    """A node's capacity roster (name → entry input_as), as boot injects it."""
    return PeerRoster(
        agents=[
            PeerCatalogEntry(agent=name, input_as=input_as)
            for name, input_as in sorted(entries.items())
        ]
    )


A_ROSTER = _roster({"utter": UTTER_AS, "audit": AUDIT_AS})
B_ROSTER = _roster({"refine": REFINE_AS, "brief": BRIEF_AS})
C_ROSTER = _roster({"spot": SPOT_AS})


async def _open_db(tmp_path: Path, name: str) -> AsyncBeaverDB:
    db = AsyncBeaverDB(str(tmp_path / f"wire-{name}.db"))
    await db.connect()
    return db


async def _pumps(runtime, count: int) -> list[asyncio.Task]:
    async def _loop() -> None:
        while True:
            await runtime.manager.run()
            await asyncio.sleep(0)

    return [asyncio.create_task(_loop()) for _ in range(count)]


async def _cancel(pumps: list[asyncio.Task]) -> None:
    for pump in pumps:
        pump.cancel()
    await asyncio.gather(*pumps, return_exceptions=True)


async def _wait_completed(runtime, task_id: str, client_id: str) -> dict:
    """Poll the author's status until the flow completes (bounded, rule 8)."""
    for _ in range(200):
        entry = await runtime.status(task_id, client_id)
        if entry.state is TaskState.COMPLETED:
            return dict(entry.output)
        await asyncio.sleep(0.01)
    raise AssertionError(f"task {task_id} did not complete in time")





# --- the 3-node example -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_delegates_to_b_and_c_and_receives_the_final_result(
    tmp_path: Path,
) -> None:
    """A's ``audit`` fans out ``[spot, refine]`` across the mesh and the author
    drains the final result — everything over the federation token."""
    mesh = MeshTransport()
    mesh_client = httpx.AsyncClient(transport=mesh)

    db_a = await _open_db(tmp_path, "a")
    db_b = await _open_db(tmp_path, "b")
    db_c = await _open_db(tmp_path, "c")
    pumps_a: list[asyncio.Task] = []
    pumps_b: list[asyncio.Task] = []
    pumps_c: list[asyncio.Task] = []
    try:
        dirs_a = _node_dirs(tmp_path, "a", peers=[(NODE_B, HOST_B), (NODE_C, HOST_C)])
        dirs_b = _node_dirs(tmp_path, "b", peers=[(NODE_C, HOST_C), (NODE_A, HOST_A)])
        dirs_c = _node_dirs(tmp_path, "c", peers=[(NODE_A, HOST_A), (NODE_B, HOST_B)])
        loaded_a = load(
            dirs_a["config"], env={"LEGIO_FEDERATION_TOKEN": TOKEN}
        )
        loaded_b = load(
            dirs_b["config"], env={"LEGIO_FEDERATION_TOKEN": TOKEN}
        )
        loaded_c = load(
            dirs_c["config"], env={"LEGIO_FEDERATION_TOKEN": TOKEN}
        )

        booted_a = await boot_node(
            loaded_a,
            db=db_a,
            composite_classes={"audit": MergeComposite},
            peer_catalogs={NODE_B: B_ROSTER, NODE_C: C_ROSTER},
            federation_client=mesh_client,
        )
        booted_b = await boot_node(
            loaded_b,
            db=db_b,
            lingo_factory=lingo_factory({"raw": "cooked-b"}),
            composite_classes={"brief": MergeComposite},
            peer_catalogs={NODE_A: A_ROSTER, NODE_C: C_ROSTER},
            federation_client=mesh_client,
        )
        booted_c = await boot_node(
            loaded_c,
            db=db_c,
            lingo_factory=lingo_factory({"raw": "watched-c"}),
            peer_catalogs={NODE_A: A_ROSTER, NODE_B: B_ROSTER},
            federation_client=mesh_client,
        )
        mesh.apps["node-a.test"] = booted_a.app
        mesh.apps["node-b.test"] = booted_b.app
        mesh.apps["node-c.test"] = booted_c.app

        pumps_a = await _pumps(booted_a.runtime, 4)
        pumps_b = await _pumps(booted_b.runtime, 3)
        pumps_c = await _pumps(booted_c.runtime, 2)

        # Every node brings its own classes up (standing loops, real bring-up).
        for spec_name in ("utter", "audit"):
            await booted_a.runtime.create_class(booted_a.catalog.specs[spec_name], pool=1)
        for spec_name in ("refine", "brief"):
            await booted_b.runtime.create_class(booted_b.catalog.specs[spec_name], pool=1)
        await booted_c.runtime.create_class(booted_c.catalog.specs["spot"], pool=1)

        # Composite dependencies are the LOCAL steps: ``audit`` names only-remote
        # branches yet is born enabled (§C).
        assert await booted_a.runtime.registry.class_dependencies("audit") == []
        assert await booted_a.runtime.registry.dependencies_satisfied("audit")
        assert await booted_a.runtime.registry.class_state("audit") == ActivityState.ENABLED
        assert await booted_b.runtime.registry.class_dependencies("brief") == []

        clear_wire(mesh)
        task_a = await booted_a.runtime.submit(
            "example-client", (("audit", AUDIT_AS),), {"raw": "hello"}
        )
        output = await _wait_completed(booted_a.runtime, task_a, "example-client")

        assert output == {"result": {"raw": "cooked-b"}}  # the B leaf's record
        assert_deposits_wire(
            mesh,
            {
                ("node-c.test", "legio:queue:spot"),
                ("node-b.test", "legio:queue:refine"),
                ("node-a.test", "legio:queue:gather:audit"),
            },
        )
    finally:
        await _cancel(pumps_a)
        await _cancel(pumps_b)
        await _cancel(pumps_c)
        await db_a.close()
        await db_b.close()
        await db_c.close()
        await mesh_client.aclose()


@pytest.mark.asyncio
async def test_b_triggers_a_the_same_way(tmp_path: Path) -> None:
    """Symmetric half: B's ``brief`` fans out ``[spot, utter]`` — C then A —
    and B alone drains its accepted final result."""
    mesh = MeshTransport()
    mesh_client = httpx.AsyncClient(transport=mesh)

    db_a = await _open_db(tmp_path, "a")
    db_b = await _open_db(tmp_path, "b")
    db_c = await _open_db(tmp_path, "c")
    pumps_a: list[asyncio.Task] = []
    pumps_b: list[asyncio.Task] = []
    pumps_c: list[asyncio.Task] = []
    try:
        dirs_a = _node_dirs(tmp_path, "a", peers=[(NODE_B, HOST_B), (NODE_C, HOST_C)])
        dirs_b = _node_dirs(tmp_path, "b", peers=[(NODE_C, HOST_C), (NODE_A, HOST_A)])
        dirs_c = _node_dirs(tmp_path, "c", peers=[(NODE_A, HOST_A), (NODE_B, HOST_B)])
        booted_a = await boot_node(
            load(dirs_a["config"], env={"LEGIO_FEDERATION_TOKEN": TOKEN}),
            db=db_a,
            composite_classes={"audit": MergeComposite},
            peer_catalogs={NODE_B: B_ROSTER, NODE_C: C_ROSTER},
            federation_client=mesh_client,
        )
        booted_b = await boot_node(
            load(dirs_b["config"], env={"LEGIO_FEDERATION_TOKEN": TOKEN}),
            db=db_b,
            lingo_factory=lingo_factory({"raw": "cooked-b"}),
            composite_classes={"brief": MergeComposite},
            peer_catalogs={NODE_A: A_ROSTER, NODE_C: C_ROSTER},
            federation_client=mesh_client,
        )
        booted_c = await boot_node(
            load(dirs_c["config"], env={"LEGIO_FEDERATION_TOKEN": TOKEN}),
            db=db_c,
            lingo_factory=lingo_factory({"raw": "watched-c"}),
            peer_catalogs={NODE_A: A_ROSTER, NODE_B: B_ROSTER},
            federation_client=mesh_client,
        )
        mesh.apps["node-a.test"] = booted_a.app
        mesh.apps["node-b.test"] = booted_b.app
        mesh.apps["node-c.test"] = booted_c.app

        pumps_a = await _pumps(booted_a.runtime, 4)
        pumps_b = await _pumps(booted_b.runtime, 3)
        pumps_c = await _pumps(booted_c.runtime, 2)

        for spec_name in ("utter", "audit"):
            await booted_a.runtime.create_class(booted_a.catalog.specs[spec_name], pool=1)
        for spec_name in ("refine", "brief"):
            await booted_b.runtime.create_class(booted_b.catalog.specs[spec_name], pool=1)
        await booted_c.runtime.create_class(booted_c.catalog.specs["spot"], pool=1)

        clear_wire(mesh)
        task_b = await booted_b.runtime.submit(
            "example-client", (("brief", BRIEF_AS),), {"raw": "hello"}
        )
        output = await _wait_completed(booted_b.runtime, task_b, "example-client")

        assert output == {"result": {"raw": "WATCHED-C!"}}  # the A tool's echo
        assert_deposits_wire(
            mesh,
            {
                ("node-c.test", "legio:queue:spot"),
                ("node-a.test", "legio:queue:utter"),
                ("node-b.test", "legio:queue:gather:brief"),
            },
        )
    finally:
        await _cancel(pumps_a)
        await _cancel(pumps_b)
        await _cancel(pumps_c)
        await db_a.close()
        await db_b.close()
        await db_c.close()
        await mesh_client.aclose()


@pytest.mark.asyncio
async def test_roster_wire_carries_entry_input_as(tmp_path: Path) -> None:
    """§A live proof: ``fetch_peer_catalogs`` over the mesh parses ``input_as``."""
    mesh = MeshTransport()
    mesh_client = httpx.AsyncClient(transport=mesh)
    db = await _open_db(tmp_path, "b")
    try:
        dirs = _node_dirs(tmp_path, "b", peers=[(NODE_A, HOST_A), (NODE_C, HOST_C)])
        booted = await boot_node(
            load(dirs["config"], env={"LEGIO_FEDERATION_TOKEN": TOKEN}),
            db=db,
            lingo_factory=lingo_factory({"raw": "cooked-b"}),
            composite_classes={"brief": MergeComposite},
            peer_catalogs={NODE_A: A_ROSTER, NODE_C: C_ROSTER},
            federation_client=mesh_client,
        )
        mesh.apps["node-b.test"] = booted.app
        rosters = await fetch_peer_catalogs({NODE_B: HOST_B}, TOKEN, client=mesh_client)
        by_name = {entry.agent: entry for entry in rosters[NODE_B].agents}
        assert by_name["refine"].input_as == REFINE_AS
        assert by_name["brief"].input_as == BRIEF_AS
        assert by_name["refine"].agent == "refine"
    finally:
        await mesh_client.aclose()
        await db.close()