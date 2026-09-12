"""`legio.api` — the node's external API surface (LEG-025, over LEG-085).

Exposes the ``Runtime`` over REST (FastAPI): ``POST /submit`` creates a task and
``GET /status/{task_id}`` reads it back with ownership enforced (LEG-014/017
semantics). The API is *polling-only*: a client creates a task and later polls
``status``; there are no callbacks. HTTP error mapping follows the LEG-016
taxonomy (4xx/5xx with a stable ``code``).

``create_app`` is auth-ready (LEG-027): when a ``ClientTokenStore`` is provided,
``submit``/``status`` are guarded per the LEG-017 two-level token model — a valid
client token is required (401 otherwise), a restricted token may only hit its
listed starting agents (403 otherwise), and ``status`` is readable only by the
token that owns the task. Without a store the app is open (``client_id`` comes
from the request) for embedded/unauthenticated contexts.

An optional ``pattern_catalog`` can be provided to derive the starting route
from the pattern catalog (LEG-021). If not provided, the agent name is used as
a single-agent route.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import FastAPI, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from legio.errors import RecoverableError, UnknownAgentError, UnrecoverableError
from legio.flow import FlowToken
from legio.patterns import Catalog, starting_route
from legio.runtime import Runtime, TaskEntry, TaskState
from legio.security import ClientTokenStore

logger = logging.getLogger(__name__)

_BEARER = re.compile(r"^Bearer\s+(.+)$")


class SubmitRequest(BaseModel):
    """Body of ``POST /submit``.

    ``agent`` is the *starting agent* (an entry point of the node, ARCH §6);
    ``payload`` is the client payload the synthetic parent stages. ``client_id``
    is only used in open (unauthenticated) mode; when a store guards the app the
    client is derived from the presented token.
    """

    client_id: str | None = Field(default=None, min_length=1)
    agent: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class SubmitResponse(BaseModel):
    task_id: str


class StatusResponse(BaseModel):
    task_id: str
    owner: str
    state: TaskState
    token: FlowToken
    output: dict[str, Any] | None = None
    result_key: str | None = None


def _to_status_response(entry: TaskEntry) -> StatusResponse:
    return StatusResponse(
        task_id=entry.task_id,
        owner=entry.owner,
        state=entry.state,
        token=entry.token,
        output=entry.output,
        result_key=entry.result_key,
    )


def _bearer_token(authorization: str | None) -> str | None:
    """Extract the bearer token from an ``Authorization`` header, if any."""
    if not authorization:
        return None
    match = _BEARER.match(authorization)
    return match.group(1) if match else None


def _unauthorized() -> JSONResponse:
    return JSONResponse(status_code=401, content={"code": "unauthorized"})


def _forbidden() -> JSONResponse:
    return JSONResponse(status_code=403, content={"code": "forbidden"})


def _resolve_route(agent_name: str, catalog: Catalog | None) -> tuple[tuple[str, str], ...]:
    """Resolve the starting route for an agent.

    If a pattern catalog is provided, look up the agent as a starting pattern
    and derive the route via ``starting_route``. An agent the catalog knows but
    has invalidated is **not served** (LEG-070): routing to it is a visible
    error, never a silent fallback. An agent the catalog does not know — or
    knows but is not ``main: true`` — is *entry-incapable*: routing to it is the
    same visible error (no silent one-task fallback that would leave a minted
    task no standing vehicle ever completes). Without a catalog no declared
    input contract exists to read a real ``input_as``, so the agent name is
    returned as a single-agent route.
    """
    if catalog is not None:
        spec = catalog.specs.get(agent_name)
        if spec is None:
            raise UnknownAgentError(
                f"unknown starting agent for catalog: {agent_name!r}"
            )
        if not catalog.is_served(agent_name):
            raise UnrecoverableError(
                f"agent not served by catalog: {agent_name!r} (invalid/disabled)"
            )
        if spec.main:
            return starting_route(spec)
        raise UnknownAgentError(
            f"agent cannot start (not main): {agent_name!r}"
        )
    return ((agent_name, agent_name),)


def create_app(
    runtime: Runtime,
    clients: ClientTokenStore | None = None,
    pattern_catalog: Catalog | None = None,
) -> FastAPI:
    """Build the FastAPI application exposing the Runtime's submit/status over REST.

    ``runtime`` is the node's orchestrator (LEG-085): the endpoints delegate
    ``submit``/``status`` to it, so the REST layer stays a thin mapping shell.

    When ``clients`` is given, ``submit``/``status`` require a valid client
    token (LEG-027); otherwise the app is open and ``client_id`` comes from the
    request body/query.

    When ``pattern_catalog`` is given, the starting route for the agent is
    derived from the pattern catalog (the agent must be marked ``main: true``);
    an unknown, non-``main`` or invalidated starting agent is refused with a
    typed error. If not provided, the agent name is used as a single-agent route.
    """
    app = FastAPI(title="legio", version="0.1.0")

    @app.post("/submit", response_model=SubmitResponse)
    async def submit(
        body: SubmitRequest,
        authorization: str | None = Header(default=None),
    ) -> SubmitResponse | JSONResponse:
        if clients is not None:
            token = _bearer_token(authorization)
            consumer_id = clients.resolve_consumer_id(token) if token else None
            if consumer_id is None:
                logger.warning("api submit unauthorized agent=%s", body.agent)
                return _unauthorized()
            if not clients.allowed_starting_agent(consumer_id, body.agent):
                logger.warning("api submit forbidden agent=%s client=%s", body.agent, consumer_id)
                return _forbidden()
            client_id = consumer_id
        else:
            client_id = body.client_id or "default"

        try:
            route = _resolve_route(body.agent, pattern_catalog)
            task_id = await runtime.submit(client_id, route, body.payload)
        except UnknownAgentError as exc:
            logger.warning("api submit unknown agent=%s reason=%s", body.agent, exc)
            return JSONResponse(status_code=422, content={"code": "unknown_agent"})
        except RecoverableError as exc:
            logger.warning("api submit denied agent=%s reason=%s", body.agent, exc)
            return JSONResponse(status_code=409, content={"code": "class_disabled"})
        except UnrecoverableError as exc:
            logger.warning("api submit rejected agent=%s reason=%s", body.agent, exc)
            return JSONResponse(status_code=422, content={"code": "invalid_request"})
        logger.info("api submit task=%s client=%s agent=%s route=%s", task_id, client_id, body.agent, ",".join(c for c, _ in route))
        return SubmitResponse(task_id=task_id)

    @app.get("/status/{task_id}", response_model=StatusResponse)
    async def status(
        task_id: str,
        authorization: str | None = Header(default=None),
        client_id: str | None = Query(default=None),
    ) -> StatusResponse | JSONResponse:
        if clients is not None:
            token = _bearer_token(authorization)
            consumer_id = clients.resolve_consumer_id(token) if token else None
            if consumer_id is None:
                logger.warning("api status unauthorized task=%s", task_id)
                return _unauthorized()
            resolved_client = consumer_id
        else:
            resolved_client = client_id

        try:
            entry = await runtime.status(task_id, resolved_client)
        except PermissionError:
            logger.warning("api status denied task=%s client=%s", task_id, resolved_client)
            return JSONResponse(status_code=403, content={"code": "access_denied"})
        except KeyError:
            logger.warning("api status unknown task=%s", task_id)
            return JSONResponse(status_code=404, content={"code": "unknown_task"})
        except RecoverableError as exc:
            logger.warning("api status failed task=%s reason=%s", task_id, exc)
            return JSONResponse(status_code=409, content={"code": "task_failed"})
        return _to_status_response(entry)

    return app


__all__ = ["StatusResponse", "SubmitRequest", "SubmitResponse", "create_app"]