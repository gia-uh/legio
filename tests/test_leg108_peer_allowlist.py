"""LEG-108 — peer allowlist enforced + honest default posture (red-first tests).

Two contracts:
1. A configured inbound peer allowlist is enforced on the **served** surface:
   an identified caller outside it is rejected 403 on every federation endpoint;
   a listed caller is admitted; no `X-Peer-Id` is admitted (single-token mode).
2. `api.host` defaults to loopback.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from legio.api import PEER_ID_HEADER, create_app
from legio.config import load
from legio.materializer import boot_node

FEDERATION_TOKEN = "fed-secret"
ALLOWED_PEER = "peer-a@host-01"
UNKNOWN_PEER = "peer-x@host-09"


async def _make_app(tmp_path: Path, allowlist: list[str]):
    tool_dir = tmp_path / "tool"
    tool_dir.mkdir()
    (tool_dir / "ok.yaml").write_text(
        """name: ok
type: atomic
kind: tool
main: true
input:
  input_as: in
  input_type: json
  input_schema: {}
output:
  output_as: out
  output_type: json
  output_schema: {}
tool: ok_tool
parameters: {}
""",
        encoding="utf-8",
    )
    for empty in ("linguistic", "composite"):
        (tmp_path / empty).mkdir()
    (tmp_path / "tools.yaml").write_text(
        """available_tools:
  ok_tool:
    implementation: tests.tools.transform
    policy:
      timeout: 10
      retries: 0
""",
        encoding="utf-8",
    )
    config = tmp_path / "legio.yaml"
    config.write_text(
        f"""node:
  id: "node-a@host-01"
database:
  db_path: "{tmp_path / "node.db"}"
patterns:
  tool: "{tool_dir}"
  linguistic: "{tmp_path / "linguistic"}"
  composite: "{tmp_path / "composite"}"
tools:
  config: "{tmp_path / "tools.yaml"}"
federation:
  allowlist: [{", ".join(f'"{p}"' for p in allowlist)}]
lifecycle:
  default:
    drain_timeout: 5.0
    drain_interval: 0.01
""",
        encoding="utf-8",
    )
    loaded = load(config)
    booted = await boot_node(loaded)
    app = create_app(
        runtime=booted.runtime,
        pattern_catalog=booted.catalog,
        federation_token=FEDERATION_TOKEN,
        peer_allowlist=allowlist,
    )
    return booted, app


FEDERATION_CALLS = [
    ("GET", "/catalog", None),
    ("GET", "/health", None),
    ("GET", "/outbox/node-b@host-02:1234-abcd", None),
    ("DELETE", "/outbox/node-b@host-02:1234-abcd", None),
    ("POST", "/deposits", {"queue": "legio:queue:ok", "item": {}, "schema_version": 1000}),
    (
        "POST",
        "/work-items/ok",
        {"task_id": "node-b@host-02:1", "payload": {}, "schema_version": 1000},
    ),
]


@pytest.mark.asyncio
async def test_unknown_peer_rejected_on_every_federation_endpoint(tmp_path: Path) -> None:
    booted, app = await _make_app(tmp_path, allowlist=[ALLOWED_PEER])
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://a") as ac:
            headers = {
                "Authorization": f"Bearer {FEDERATION_TOKEN}",
                PEER_ID_HEADER: UNKNOWN_PEER,
            }
            for method, url, body in FEDERATION_CALLS:
                resp = await ac.request(method, url, json=body, headers=headers)
                assert resp.status_code == 403, (method, url, resp.status_code, resp.text)
                assert resp.json()["code"] == "forbidden"
    finally:
        await booted.db.close()


@pytest.mark.asyncio
async def test_allowed_peer_admitted_and_anonymous_admitted(tmp_path: Path) -> None:
    booted, app = await _make_app(tmp_path, allowlist=[ALLOWED_PEER])
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://a") as ac:
            auth = {"Authorization": f"Bearer {FEDERATION_TOKEN}"}
            allowed = await ac.get("/catalog", headers={**auth, PEER_ID_HEADER: ALLOWED_PEER})
            assert allowed.status_code == 200, allowed.text
            anonymous = await ac.get("/catalog", headers=auth)
            assert anonymous.status_code == 200, anonymous.text
            bad_token = await ac.get("/catalog", headers={PEER_ID_HEADER: ALLOWED_PEER})
            assert bad_token.status_code == 401
    finally:
        await booted.db.close()


@pytest.mark.asyncio
async def test_empty_allowlist_admits_any_identified_peer(tmp_path: Path) -> None:
    booted, app = await _make_app(tmp_path, allowlist=[])
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://a") as ac:
            resp = await ac.get(
                "/catalog",
                headers={
                    "Authorization": f"Bearer {FEDERATION_TOKEN}",
                    PEER_ID_HEADER: UNKNOWN_PEER,
                },
            )
            assert resp.status_code == 200, resp.text
    finally:
        await booted.db.close()


def test_api_host_defaults_to_loopback() -> None:
    from legio.config import ApiConfig

    assert ApiConfig().host == "127.0.0.1"


def test_runtime_accepts_peer_allowlist_in_create_app() -> None:
    # Signature guard: create_app exposes the peer_allowlist seam.
    import inspect

    params = inspect.signature(create_app).parameters
    assert "peer_allowlist" in params
