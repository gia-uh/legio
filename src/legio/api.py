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

Federation (LEG-090/092/093/095): when ``federation_token`` is provided, the app
also serves ``GET /catalog`` (the node's roster of served capacity, guarded by
the shared federation token), ``POST /work-items/{agent}`` (remote deposit) and
the outbox verbs ``GET``/``DELETE /outbox/{task_id}`` (result readback/ack for
remote work). A peer reads ``/catalog`` to author remote work (LEG-091/092);
results land on the task's outbox (the result queue) and the author polls and
acks them here (LEG-093). ``POST /deposits`` is the owner's half of the
node-injected db proxy (LEG-095 Phase 3): an author's ``NodeDB`` routes a
foreign queue name here and the owner performs the local ``put`` — the remote
leg is pure transport. Without a federation token none of the endpoints are
mounted (404): no federation surface.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import FastAPI, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from legio.errors import (
    InvalidNameError,
    RecoverableError,
    UnknownAgentError,
    UnrecoverableError,
)
from legio.flow import SCHEMA_VERSION, FlowToken
from legio.naming import QUEUE_NAMESPACE, validate_task_id
from legio.patterns import Catalog, starting_route
from legio.runtime import Runtime, TaskEntry, TaskState
from legio.security import ClientTokenStore, FederationTokenStore

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


class CatalogAgentInterface(BaseModel):
    """The versioned capability a node advertises for one served agent."""

    capability: str
    schema_version: int


class CatalogAgentEntry(BaseModel):
    """One served agent in the node's catalog (LEG-090)."""

    agent: str
    interface: CatalogAgentInterface
    kind: str


class CatalogResponse(BaseModel):
    """The node's roster of served capacity (LEG-090)."""

    schema_version: int
    agents: list[CatalogAgentEntry]


class WorkItemRequest(BaseModel):
    """Body of ``POST /work-items/{agent}`` (LEG-092).

    The author mints ``task_id`` (``<node_id>:<uuid>``) so the result lands
    where it reads it and so a retried delivery is idempotent; ``payload`` is
    the work's inputs; ``schema_version`` is the flow schema version the author
    advertised for this agent in its roster read (LEG-090). Extra fields are
    forbidden — a lifecycle verb is simply not part of the work-item contract
    (federation transports work, never lifecycle, session 85q).
    """

    model_config = {"extra": "forbid"}

    task_id: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    schema_version: int


class WorkItemResponse(BaseModel):
    """The acceptor's receipt for a deposited work item (LEG-092)."""

    id: str
    deposited: bool
    deduplicated: bool = False


class OutboxPollResponse(BaseModel):
    """The author's non-blocking poll of a work item's outbox (LEG-093).

    ``ready`` is true once the acceptor's flow has written the
    ``ExecutionResultMessage`` to the task's result queue — the write always
    precedes any ack (write-before-ack is structural), and this poll never
    blocks or pushes (rule 8).
    """

    id: str
    ready: bool
    result: dict[str, Any] | None = None


class OutboxAckResponse(BaseModel):
    """The author's destructive ack of a work item's outbox (LEG-093).

    ``acked`` is true when the result was consumed; false when the outbox was
    already empty (idempotent ack — never an error).
    """

    id: str
    acked: bool


class DepositRequest(BaseModel):
    """Body of ``POST /deposits`` (LEG-095 Phase 3).

    The proxy's remote leg deposits a message onto a full flow queue name
    (``legio:queue:<...>``); the owner performs the local ``put``. ``item`` is a
    dict (a flow message's JSON shape — the endpoint is transport, it does not
    mint lifecycle); ``priority`` passes through (agents always send ``0.0``).
    Extra fields are forbidden — federation transports work, never lifecycle.
    """

    model_config = {"extra": "forbid"}

    queue: str = Field(min_length=1)
    item: dict[str, Any]
    priority: float = 0.0


class DepositResponse(BaseModel):
    """The owner's receipt for a federated queue deposit (LEG-095 Phase 3)."""

    queue: str
    deposited: bool


def _to_catalog_response(catalog: Catalog) -> CatalogResponse:
    """Derive the node's capacity roster from its served pattern catalog.

    Capacity is *not* a separate register: the node can execute exactly what
    its served pattern catalog materializes (LEG-070/081). Each served agent is
    advertised under its name with the node's flow schema version; ``kind`` is
    the agent kind (tool/linguistic) or ``composite`` for flows.
    """
    entries: list[CatalogAgentEntry] = []
    for name in sorted(catalog.served()):
        spec = catalog.specs[name]
        kind = spec.kind.value if spec.kind is not None else "composite"
        entries.append(
            CatalogAgentEntry(
                agent=name,
                interface=CatalogAgentInterface(
                    capability=name,
                    schema_version=SCHEMA_VERSION,
                ),
                kind=kind,
            )
        )
    return CatalogResponse(schema_version=SCHEMA_VERSION, agents=entries)


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


def _guard_outbox(
    task_id: str,
    federation_store: FederationTokenStore | None,
    token: str | None,
) -> str | JSONResponse:
    """L1 + identifier guard shared by both outbox verbs (LEG-093).

    Both ``GET``/``DELETE /outbox/{task_id}`` need the shared federation token
    (401) and a well-formed author task id (422) before touching the outbox
    record. Returns the validated ``task_id`` or a JSON error.
    """
    if federation_store is None or token is None or not federation_store.is_valid(token):
        logger.warning("api outbox unauthorized task=%s", task_id)
        return _unauthorized()
    try:
        validate_task_id(task_id)
    except InvalidNameError as exc:
        logger.warning("api outbox invalid_id task=%s reason=%s", task_id, exc)
        return JSONResponse(status_code=422, content={"code": "invalid_request"})
    return task_id


def _deposit_agent(queue_name: str) -> str | None:
    """The served agent a deposit addresses, or ``None`` for result queues.

    Agent queues (``legio:queue:<agent>``) address their agent; gather queues
    (``legio:queue:gather:<composite>``) address the composite whose branches
    return through it; result queues (``legio:queue:result:<...>``) are pure
    transport — the drain reads them by served root agent, and a result-bearing
    deposit must never be refused on a served check (LEG-095 Phase 3 §B).
    """
    relative = queue_name[len(QUEUE_NAMESPACE):]
    if relative.startswith("gather:"):
        return relative[len("gather:"):]
    if relative.startswith("result:"):
        return None
    return relative


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
    federation_token: str | None = None,
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

    When ``federation_token`` is provided, the app serves ``GET /catalog``
    (LEG-090), ``POST /work-items/{agent}`` (LEG-092) and the outbox verbs
    ``GET``/``DELETE /outbox/{task_id}`` (LEG-093), all guarded by the shared
    token; absent the endpoints are not mounted (no federation surface).
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

    if federation_token is not None:
        federation_store = FederationTokenStore(federation_token)

        @app.get("/catalog", response_model=CatalogResponse)
        async def catalog(
            authorization: str | None = Header(default=None),
        ) -> CatalogResponse | JSONResponse:
            token = _bearer_token(authorization)
            if token is None or not federation_store.is_valid(token):
                logger.warning("api catalog unauthorized")
                return _unauthorized()
            if pattern_catalog is None:
                logger.error("api catalog no capacity")
                return JSONResponse(status_code=503, content={"code": "no_capacity"})
            return _to_catalog_response(pattern_catalog)

        @app.post("/work-items/{agent}", response_model=WorkItemResponse)
        async def work_item(
            agent: str,
            body: WorkItemRequest,
            authorization: str | None = Header(default=None),
        ) -> WorkItemResponse | JSONResponse:
            token = _bearer_token(authorization)
            if token is None or not federation_store.is_valid(token):
                logger.warning("api work_item unauthorized agent=%s", agent)
                return _unauthorized()
            if pattern_catalog is None:
                logger.error("api work_item no capacity agent=%s", agent)
                return JSONResponse(status_code=503, content={"code": "no_capacity"})
            if not pattern_catalog.is_served(agent):
                logger.warning("api work_item unknown agent=%s", agent)
                return JSONResponse(status_code=404, content={"code": "unknown_agent"})
            if body.schema_version != SCHEMA_VERSION:
                logger.warning(
                    "api work_item interface_mismatch agent=%s schema=%s",
                    agent,
                    body.schema_version,
                )
                return JSONResponse(status_code=409, content={"code": "interface_mismatch"})
            try:
                validate_task_id(body.task_id)
            except InvalidNameError as exc:
                logger.warning("api work_item invalid_id agent=%s reason=%s", agent, exc)
                return JSONResponse(status_code=422, content={"code": "invalid_request"})
            author = body.task_id.split(":", 1)[0]
            route = starting_route(pattern_catalog.specs[agent])
            try:
                receipt = await runtime.submit_work_item(
                    author, route, body.payload, task_id=body.task_id
                )
            except RecoverableError as exc:
                logger.warning("api work_item denied agent=%s reason=%s", agent, exc)
                return JSONResponse(status_code=409, content={"code": "class_disabled"})
            logger.info(
                "api work_item task=%s owner=%s agent=%s deposited=%s deduplicated=%s",
                receipt.id,
                author,
                agent,
                receipt.deposited,
                receipt.deduplicated,
            )
            return WorkItemResponse(
                id=receipt.id,
                deposited=receipt.deposited,
                deduplicated=receipt.deduplicated,
            )

        @app.get("/outbox/{task_id}", response_model=OutboxPollResponse)
        async def outbox_poll(
            task_id: str,
            authorization: str | None = Header(default=None),
        ) -> OutboxPollResponse | JSONResponse:
            auth_data = _guard_outbox(task_id, federation_store, _bearer_token(authorization))
            if isinstance(auth_data, JSONResponse):
                return auth_data
            payload = await runtime.read_outbox(task_id)
            ready = payload is not None
            logger.info("api outbox poll task=%s ready=%s", task_id, ready)
            return OutboxPollResponse(id=task_id, ready=ready, result=payload)

        @app.delete("/outbox/{task_id}", response_model=OutboxAckResponse)
        async def outbox_ack(
            task_id: str,
            authorization: str | None = Header(default=None),
        ) -> OutboxAckResponse | JSONResponse:
            auth_data = _guard_outbox(task_id, federation_store, _bearer_token(authorization))
            if isinstance(auth_data, JSONResponse):
                return auth_data
            acked = await runtime.ack_outbox(task_id)
            logger.info("api outbox ack task=%s acked=%s", task_id, acked)
            return OutboxAckResponse(id=task_id, acked=acked)

        @app.post("/deposits", response_model=DepositResponse)
        async def deposits(
            body: DepositRequest,
            authorization: str | None = Header(default=None),
        ) -> DepositResponse | JSONResponse:
            """The owner's half of a routed queue deposit (LEG-095 Phase 3).

            The author's proxy (a ``NodeDB``) deposits a message onto a flow
            queue name through this endpoint; the owner performs the local
            ``put`` — no direct remote write ever exists. Validation order
            (visible, rule 9): L1 bearer → 401; no catalog → 503
            (``no_capacity``, a federated node must never serve silently
            empty); malformed queue name → 422; agent/gather queue for an
            unserved agent → 404; gate denial → 409; deposit → 200.
            """
            token = _bearer_token(authorization)
            if token is None or not federation_store.is_valid(token):
                logger.warning("api deposits unauthorized queue=%s", body.queue)
                return _unauthorized()
            if pattern_catalog is None:
                logger.error("api deposits no capacity queue=%s", body.queue)
                return JSONResponse(status_code=503, content={"code": "no_capacity"})
            if not body.queue.startswith(QUEUE_NAMESPACE) or len(body.queue) <= len(
                QUEUE_NAMESPACE
            ):
                logger.warning("api deposits invalid_request queue=%r", body.queue)
                return JSONResponse(status_code=422, content={"code": "invalid_request"})
            agent = _deposit_agent(body.queue)
            if agent is not None and not pattern_catalog.is_served(agent):
                logger.warning("api deposits unknown_agent queue=%s", body.queue)
                return JSONResponse(status_code=404, content={"code": "unknown_agent"})
            try:
                await runtime.deposit_remote(body.queue, body.item, body.priority)
            except RecoverableError as exc:
                logger.warning("api deposits denied queue=%s reason=%s", body.queue, exc)
                return JSONResponse(status_code=409, content={"code": "class_disabled"})
            logger.info("api deposits queue=%s deposited=true", body.queue)
            return DepositResponse(queue=body.queue, deposited=True)

    return app


__all__ = [
    "CatalogAgentEntry",
    "CatalogAgentInterface",
    "CatalogResponse",
    "DepositRequest",
    "DepositResponse",
    "OutboxAckResponse",
    "OutboxPollResponse",
    "StatusResponse",
    "SubmitRequest",
    "SubmitResponse",
    "WorkItemRequest",
    "WorkItemResponse",
    "create_app",
]