"""Contract tests for LEG-025 — the agent loop over the Runtime REST.

The REST surface exposes ``POST /submit`` and ``GET /status/{task_id}`` that
delegate to the Runtime (LEG-085), enforcing ownership (a foreign client's
status request is denied). Uses the ASGI transport with an async client (httpx)
to exercise the real FastAPI routes. All substrate is native beaver, bound via
the ``beaver_db`` fixture.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from beaver import AsyncBeaverDB

from legio.agents.tool_agent import ToolAgent
from legio.api import create_app
from legio.patterns import Catalog, load_patterns
from legio.runtime import Runtime
from legio.tools import AvailableToolsRegistry


def fake_flip(text: str) -> dict:
    """Domain-free fake tool: plain callable, signature is its contract."""
    return {"flipped": str(text)[::-1]}


# Dependency chain for catalog-driven routing: gatekeeper (main) -> leaf.
# Invalidating leaf removes gatekeeper from the served catalog (LEG-070).
CHAIN_YAML = """
name: leaf
type: atomic
kind: linguistic
main: false
input:
  input_as: text
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
output:
  output_as: leaf
  output_type: json
  output_schema:
    type: object
    properties:
      leaf: {type: string}
prompt: "rewrite the text: {text}"
---
name: gatekeeper
type: composite
main: true
input:
  input_as: payload
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
output:
  output_as: result
  output_type: json
  output_schema:
    type: object
    properties:
      result: {type: string}
branches:
  - - leaf
"""


def _chain_catalog() -> Catalog:
    return load_patterns(CHAIN_YAML)


def build_flip_agent(db: AsyncBeaverDB) -> ToolAgent:
    registry = AvailableToolsRegistry()
    registry.declare(
        "flip",
        implementation="tests.test_tools.fake_flip",
        policy={"timeout": 30, "retries": 0},
    )
    return ToolAgent(
        agent_id="flip",
        db=db,
        available_tools=registry,
        tool_name="flip",
        parameters={"text": "{flip.text}"},
        input_as="flip",
        output_as="flip",
    )


@pytest.fixture
async def client(runtime: Runtime) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(runtime=runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_submit_creates_task_and_status_returns_it(
    client: httpx.AsyncClient, beaver_db: AsyncBeaverDB
) -> None:
    resp = await client.post(
        "/submit", json={"client_id": "client-a", "agent": "flow_alpha", "payload": {"raw": 1}}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"].startswith("toplevel@test:")

    task_id = body["task_id"]
    st = await client.get(f"/status/{task_id}", params={"client_id": "client-a"})
    assert st.status_code == 200, st.text
    entry = st.json()
    assert entry["task_id"] == task_id
    assert entry["owner"] == "client-a"
    assert entry["token"]["root"] is True
    assert entry["token"]["level"] == 1
    assert entry["token"]["level_route"] == [["flow_alpha", "flow_alpha"]]


@pytest.mark.asyncio
async def test_status_denies_foreign_client(
    client: httpx.AsyncClient, beaver_db: AsyncBeaverDB
) -> None:
    resp = await client.post(
        "/submit", json={"client_id": "client-a", "agent": "flow_alpha", "payload": {"raw": 1}}
    )
    task_id = resp.json()["task_id"]

    denied = await client.get(f"/status/{task_id}", params={"client_id": "client-b"})
    assert denied.status_code == 403
    assert denied.json()["code"] == "access_denied"

    anonymous = await client.get(f"/status/{task_id}")
    assert anonymous.status_code in (403, 422)


@pytest.mark.asyncio
async def test_status_unknown_task(client: httpx.AsyncClient, beaver_db: AsyncBeaverDB) -> None:
    resp = await client.get("/status/T-nope", params={"client_id": "client-a"})
    assert resp.status_code == 404
    assert resp.json()["code"] == "unknown_task"


@pytest.fixture
async def client_with_catalog(runtime: Runtime) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(runtime=runtime, pattern_catalog=_chain_catalog())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_submit_to_invalidated_agent_is_typed_error(
    runtime: Runtime, beaver_db: AsyncBeaverDB
) -> None:
    catalog = _chain_catalog()
    catalog.invalidate("leaf")
    app = create_app(runtime=runtime, pattern_catalog=catalog)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/submit",
            json={"client_id": "client-a", "agent": "gatekeeper", "payload": {"raw": 1}},
        )
    assert resp.status_code == 422, resp.text
    assert resp.json()["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_submit_to_served_main_agent_accepts_catalog_route(
    client_with_catalog: httpx.AsyncClient, beaver_db: AsyncBeaverDB
) -> None:
    resp = await client_with_catalog.post(
        "/submit",
        json={"client_id": "client-a", "agent": "gatekeeper", "payload": {"raw": 1}},
    )
    assert resp.status_code == 200, resp.text
    entry = (
        await client_with_catalog.get(
            f"/status/{resp.json()['task_id']}", params={"client_id": "client-a"}
        )
    ).json()
    assert entry["token"]["level_route"] == [["gatekeeper", "payload"]]


@pytest.mark.asyncio
async def test_submit_to_unknown_or_non_main_agent_is_typed_error(
    runtime: Runtime, beaver_db: AsyncBeaverDB
) -> None:
    app = create_app(runtime=runtime, pattern_catalog=_chain_catalog())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        unknown = await ac.post(
            "/submit",
            json={"client_id": "client-a", "agent": "ghost", "payload": {"raw": 1}},
        )
        non_main = await ac.post(
            "/submit",
            json={"client_id": "client-a", "agent": "leaf", "payload": {"raw": 1}},
        )
    assert unknown.status_code == 422, unknown.text
    assert unknown.json()["code"] == "unknown_agent"
    assert non_main.status_code == 422, non_main.text
    assert non_main.json()["code"] == "unknown_agent"


@pytest.mark.asyncio
async def test_submit_missing_fields_is_rejected(
    client: httpx.AsyncClient, beaver_db: AsyncBeaverDB
) -> None:
    resp = await client.post("/submit", json={"client_id": "client-a"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_agent_loop_deposits_message_to_completion(
    beaver_db: AsyncBeaverDB,
    runtime: Runtime,
) -> None:
    task_id = await runtime.submit("client-a", (("flip", "flip"),), {"text": "abc"})
    # The seed task deposits the root message on the node pump (§7.1).
    await runtime.manager.run()

    agent = build_flip_agent(beaver_db)

    processed = await agent.run()
    assert processed == 1

    # Phase 2: the result lands on the agent's shared queue; status schedules
    # its collection (kick-on-miss) and the node pump dispatches the drain.
    await runtime.status(task_id, "client-a")
    for _ in range(10):
        await runtime.manager.run()

    entry = await runtime.status(task_id, "client-a")
    assert entry.state.value == "completed"
    assert entry.output == {"flip": {"flipped": "cba"}}


@pytest.mark.asyncio
async def test_submit_into_disabled_class_returns_typed_error(
    client: httpx.AsyncClient, runtime: Runtime, beaver_db: AsyncBeaverDB
) -> None:
    await beaver_db.dict("gates").set("gated", {"state": "disabled"})

    resp = await client.post(
        "/submit",
        json={"client_id": "client-a", "agent": "gated", "payload": {"raw": 1}},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "class_disabled"


@pytest.mark.asyncio
async def test_status_of_failed_task_returns_failed_state(
    client: httpx.AsyncClient, runtime: Runtime, beaver_db: AsyncBeaverDB
) -> None:
    """FAILED seed is readable via status with state=FAILED (no exception raised)."""
    task_id = await runtime.submit("client-a", (("failing", "failing"),), {"raw": 1})

    tasks = beaver_db.dict("tasks")
    record = await tasks.fetch(task_id)
    record["status"] = "failed"
    record["error"] = "boom"
    await tasks.set(task_id, record)

    st = await client.get(f"/status/{task_id}", params={"client_id": "client-a"})
    # New behavior: returns 200 with state=FAILED and error in output
    assert st.status_code == 200
    data = st.json()
    assert data["state"] == "failed"
    assert "error" in data["output"]
    assert "boom" in data["output"]["error"]
