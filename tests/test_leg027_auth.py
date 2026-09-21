"""Contract tests for LEG-027 — Auth middleware over the REST surface.

Guards ``submit``/``status`` per the LEG-017 two-level token model: a valid
client token is required (401 otherwise), a restricted token may only hit its
listed starting agents (403 otherwise), ownership of ``status`` is enforced, and
revocation takes effect immediately. Tokens are never present in responses.
"""

from __future__ import annotations

import typing

import httpx
import pytest
from beaver import AsyncBeaverDB

from legio.agents.tool_agent import ToolAgent
from legio.api import create_app
from legio.runtime import Runtime
from legio.security import ClientTokenStore
from legio.tools import AvailableToolsRegistry

if typing.TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI


@pytest.fixture
def store() -> ClientTokenStore:
    s = ClientTokenStore()
    s.register("client-a", token="tok-a")  # all agents, by default
    s.register("client-b", token="tok-b", agents=["flow_b_only"])
    return s


@pytest.fixture
def app(store: ClientTokenStore, runtime: Runtime) -> FastAPI:
    return create_app(runtime=runtime, clients=store)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def ac(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_submit_requires_client_token(ac: httpx.AsyncClient) -> None:
    resp = await ac.post("/submit", json={"agent": "flow_alpha", "payload": {"raw": 1}})
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


@pytest.mark.asyncio
async def test_submit_with_valid_token_works(ac: httpx.AsyncClient) -> None:
    resp = await ac.post(
        "/submit",
        json={"agent": "flow_alpha", "payload": {"raw": 1}},
        headers=bearer("tok-a"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["task_id"].startswith("toplevel@test:")


@pytest.mark.asyncio
async def test_submit_with_wrong_or_unknown_token_denied(ac: httpx.AsyncClient) -> None:
    wrong = await ac.post("/submit", json={"agent": "flow_alpha"}, headers=bearer("tok-nope"))
    assert wrong.status_code == 401

    federation_token_denied = await ac.post(
        "/submit", json={"agent": "flow_alpha"}, headers=bearer("shared-federation-key")
    )
    assert federation_token_denied.status_code == 401


@pytest.mark.asyncio
async def test_restricted_token_cannot_submit_unlisted_agent(ac: httpx.AsyncClient) -> None:
    denied = await ac.post(
        "/submit",
        json={"agent": "flow_alpha", "payload": {}},
        headers=bearer("tok-b"),
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "forbidden"

    allowed = await ac.post(
        "/submit",
        json={"agent": "flow_b_only", "payload": {}},
        headers=bearer("tok-b"),
    )
    assert allowed.status_code == 200


@pytest.mark.asyncio
async def test_status_requires_owning_client_token(ac: httpx.AsyncClient) -> None:
    created = await ac.post(
        "/submit",
        json={"agent": "flow_alpha", "payload": {"raw": 1}},
        headers=bearer("tok-a"),
    )
    task_id = created.json()["task_id"]

    no_token = await ac.get(f"/status/{task_id}")
    assert no_token.status_code == 401

    foreign = await ac.get(f"/status/{task_id}", headers=bearer("tok-b"))
    assert foreign.status_code == 403

    owner = await ac.get(f"/status/{task_id}", headers=bearer("tok-a"))
    assert owner.status_code == 200
    assert owner.json()["owner"] == "client-a"


@pytest.mark.asyncio
async def test_revocation_takes_effect_immediately(
    ac: httpx.AsyncClient, store: ClientTokenStore
) -> None:
    ok = await ac.post("/submit", json={"agent": "flow_alpha"}, headers=bearer("tok-a"))
    assert ok.status_code == 200

    store.revoke("client-a")

    revoked = await ac.post("/submit", json={"agent": "flow_alpha"}, headers=bearer("tok-a"))
    assert revoked.status_code == 401

    unaffected = await ac.post("/submit", json={"agent": "flow_b_only"}, headers=bearer("tok-b"))
    assert unaffected.status_code == 200


@pytest.mark.asyncio
async def test_tokens_never_appear_in_responses(ac: httpx.AsyncClient) -> None:
    created = await ac.post(
        "/submit",
        json={"agent": "flow_alpha", "payload": {"raw": 1}},
        headers=bearer("tok-a"),
    )
    task_id = created.json()["task_id"]
    body = {
        "submit": created.text,
        "status": (await ac.get(f"/status/{task_id}", headers=bearer("tok-a"))).text,
    }
    for key, value in body.items():
        assert "tok-a" not in value, f"token leaked in {key}"
        assert "tok-b" not in value


def fake_transform(text: str) -> dict:
    """Domain-free fake tool: plain callable, signature is its contract."""
    return {"upper": str(text).upper()}


def build_transform_agent(db: AsyncBeaverDB) -> ToolAgent:
    registry = AvailableToolsRegistry()
    registry.declare(
        "transform",
        implementation="tests.test_tools.fake_transform",
        policy={"timeout": 30, "retries": 0},
    )
    return ToolAgent(
        agent_id="transform",
        db=db,
        available_tools=registry,
        tool_name="transform",
        parameters={"text": "{transform.text}"},
        input_as="transform",
        output_as="transform",
    )


def build_upper_agent(db: AsyncBeaverDB) -> ToolAgent:
    registry = AvailableToolsRegistry()
    registry.declare(
        "upper",
        implementation="tests.test_tools.fake_upper",
        policy={"timeout": 30, "retries": 0},
    )
    return ToolAgent(
        agent_id="upper",
        db=db,
        available_tools=registry,
        tool_name="upper",
        parameters={"text": "{upper.text}"},
        input_as="upper",
        output_as="upper",
    )


@pytest.mark.asyncio
async def test_leg026_example_runs_behind_auth(
    ac: httpx.AsyncClient, beaver_db: AsyncBeaverDB, runtime: Runtime
) -> None:
    agent = build_upper_agent(beaver_db)

    created = await ac.post(
        "/submit",
        json={"agent": "upper", "payload": {"text": "hi"}},
        headers=bearer("tok-a"),
    )
    assert created.status_code == 200, created.text
    task_id = created.json()["task_id"]
    # The seed task deposits the root message on the node pump (§7.1).
    await runtime.manager.run()

    await agent.run()

    # The result sits on the agent's shared queue; status schedules its
    # collection (kick-on-miss) and the node pump dispatches the drain.
    await ac.get(f"/status/{task_id}", headers=bearer("tok-a"))
    for _ in range(10):
        await runtime.manager.run()

    owner = await ac.get(f"/status/{task_id}", headers=bearer("tok-a"))
    assert owner.status_code == 200, owner.text
    assert owner.json()["state"] == "completed"
    assert owner.json()["output"] == {"upper": {"upper": "HI"}}


@pytest.mark.asyncio
async def test_revocation_blocks_repeat_within_e2e(
    ac: httpx.AsyncClient, store: ClientTokenStore
) -> None:
    ok = await ac.post("/submit", json={"agent": "flow_alpha"}, headers=bearer("tok-a"))
    assert ok.status_code == 200
    store.revoke("client-a")

    repeat = await ac.post("/submit", json={"agent": "flow_alpha"}, headers=bearer("tok-a"))
    assert repeat.status_code == 401
