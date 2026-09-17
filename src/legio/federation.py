"""`legio.federation` — the federated step resolver (LEG-091, R-9) and the
node-injected db proxy (LEG-095 Phase 3).

The author resolves each step's required agent **before deposit**: local when
the node serves it, remote when a configured peer's catalog offers it with a
matching ``schema_version``, error otherwise. The resolver is a *pure
decision* — no beaver, no HTTP, no deposits. The client layer (LEG-092/093)
fetches peer rosters (LEG-090 ``GET /catalog`` responses) and deposits work
items; the acceptor's agents therefore never receive work for a pattern they do
not serve (ARCH §9).

Phase 3 adds the **transport half**: the boot composes ``NodeDB`` — the
agents' single ``db`` handle, a transparent ``AsyncBeaverDB`` proxy sharing the
raw connection state (no second connection; the boot owns the lifecycle,
``isinstance(node_db, AsyncBeaverDB)`` holds). ``queue(name)`` routes *by
queue name*: internal names hit local beaver, foreign names deposit onto the
owning peer's federation-only ``POST /deposits`` (the remote leg is pure
transport — a read-less ``put`` shim; reads on foreign names are a visible
violation). The static table is boot-built by ``build_routes`` from the local
served set plus the configured peers' rosters (LEG-090), fetched by
``fetch_peer_catalogs`` — fail-fast, the peer named (rule 9). Cross-node
deposits therefore carry work, never lifecycle (ARCH §9, session 85q).

Local-first framing (ARCH §9, maintainer session 85q): federation transports
*work*, never lifecycle. A node cannot control another node's agents; the
resolver only decides whether a step executes at home or on a peer.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from beaver import AsyncBeaverDB
from beaver.dicts import AsyncBeaverDict
from beaver.queues import AsyncBeaverQueue

from legio.errors import RecoverableError, UnrecoverableError
from legio.flow import SCHEMA_VERSION
from legio.naming import QUEUE_NAMESPACE

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentInterface:
    """A versioned agent capability a node offers to (or reads from) a peer.

    Relocated here from the retired LEG-015 in-memory plane (LEG-103 Slice 4):
    the production federation reuses only this value type — no registry, no
    queues, no global state travel with it.
    """

    capability: str
    schema_version: int

#: A well-formed task id — ``<origin>:<uuid>`` (LEG-016). ``NodeDB`` parses the
#: author origin out of ``result:<task_id>`` queue names; anything that does not
#: match is a malformed/legacy shape and routes local (back-compat).
_TASK_ORIGIN_RE = re.compile(
    r"^([^:]+):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

_RESULT_PREFIX = "result:"
_GATHER_PREFIX = "gather:"


class InterfaceMismatchError(UnrecoverableError):
    """A peer advertises the agent with an incompatible ``schema_version``.

    Never silently skipped (rule 9): an offered-but-incompatible agent is a
    visible author-time failure, not a hop to the next peer.
    """


class UnresolvableAgentError(UnrecoverableError):
    """The agent is served neither locally nor offered by any configured peer.

    Resolution happens before deposit (ARCH §9) — raising here is the pre-deposit
    gate that guarantees nothing unresolvable is ever queued.
    """


@dataclass(frozen=True)
class Local:
    """The step runs on the node itself (served locally)."""

    agent: str


@dataclass(frozen=True)
class Remote:
    """The step is delegated to a peer that offers it with a matching interface."""

    peer_id: str
    agent: str
    interface: AgentInterface


class StepResolver:
    """Decide where a required agent runs, in the author's own node.

    ``local_capacity`` is the node's served capacity (``pattern_catalog.served()``
    — the same roster LEG-090 exposes). ``peer_catalogs`` maps each configured
    peer's id to its advertised roster (agent name → interface); fetching those
    rosters over HTTP is the client layer's concern (LEG-092), not the
    resolver's.
    """

    def __init__(
        self,
        local_capacity: frozenset[str],
        peer_catalogs: Mapping[str, Mapping[str, AgentInterface]] | None = None,
    ) -> None:
        self._local_capacity = frozenset(local_capacity)
        self._peer_catalogs = {peer_id: dict(roster) for peer_id, roster in (peer_catalogs or {}).items()}

    def resolve(self, agent: str) -> Local | Remote:
        """Resolve ``agent`` to a local step or a peer delegation, or raise.

        Resolution order: local catalog → peer catalogs (interface must match) →
        error. The first offering peer in the given order decides; a peer that
        offers the agent with a mismatched ``schema_version`` fails loudly
        (no silent skip — rule 9). A peer that does not offer the agent is
        simply skipped.
        """
        if agent in self._local_capacity:
            return Local(agent=agent)

        for peer_id, roster in self._peer_catalogs.items():
            offered = roster.get(agent)
            if offered is None:
                continue
            if offered.schema_version != SCHEMA_VERSION:
                logger.warning(
                    "federation resolve interface mismatch agent=%s peer=%s want=%s have=%s",
                    agent,
                    peer_id,
                    SCHEMA_VERSION,
                    offered.schema_version,
                )
                raise InterfaceMismatchError(
                    f"agent {agent!r} offered by peer {peer_id!r} with "
                    f"schema_version {offered.schema_version}; this node speaks "
                    f"schema_version {SCHEMA_VERSION}"
                )
            logger.info("federation resolve remote agent=%s peer=%s", agent, peer_id)
            return Remote(peer_id=peer_id, agent=agent, interface=offered)

        logger.warning("federation resolve unresolvable agent=%s", agent)
        raise UnresolvableAgentError(f"agent {agent!r} is served neither locally nor by any peer")


@dataclass(frozen=True)
class PeerCatalogEntry:
    """One agent a peer's roster offers (the ``GET /catalog`` wire shape).

    ``input_as`` is the agent's declared input scope (LEG-090, amended by
    LEG-094 session 86e): the author uses it as the ``(class, input_as)`` route
    step when the offer is accepted — messages are re-keyed under the executing
    agent's own ``input_as``, local or peer, identically.
    """

    agent: str
    input_as: str


@dataclass(frozen=True)
class PeerRoster:
    """A peer's advertised roster of served agents (LEG-090)."""

    agents: list[PeerCatalogEntry]


def _roster_names(roster: Any) -> Iterable[str]:
    """The agent names a roster advertises, whatever its concrete shape.

    Accepts both the LEG-090 ``CatalogResponse`` (an ``agents`` list whose
    entries carry ``.agent``) and a plain iterable of agent names, so the boot
    may feed ``fetch_peer_catalogs`` rosters or in-test dictionaries alike.
    """
    agents = getattr(roster, "agents", None)
    if agents is not None:
        return [entry.agent for entry in agents]
    return roster


def roster_steps(rosters: Mapping[str, Any]) -> dict[str, str]:
    """Flatten rosters into the ``step name → input_as`` map the loader accepts.

    First offering peer wins (the same order ``build_routes`` walks, so the
    route and the input scope never disagree). Entries without an ``input_as``
    (a pre-LEG-094 roster shape) are a visible ``RecoverableError`` — a peer
    step whose input scope is unknown cannot be routed (rule 9).
    """
    steps: dict[str, str] = {}
    for peer_id, roster in (rosters or {}).items():
        for entry in _roster_entries(roster):
            name = getattr(entry, "agent", entry)
            input_as = getattr(entry, "input_as", None)
            if input_as is None:
                logger.warning(
                    "federation roster entry missing input_as peer=%s agent=%s",
                    peer_id,
                    name,
                )
                raise RecoverableError(
                    f"peer {peer_id!r} offers agent {name!r} without an input_as "
                    "(LEG-090 roster must carry each entry's input_as)"
                )
            steps.setdefault(name, input_as)
    return steps


def _roster_entries(roster: Any) -> list[Any]:
    agents = getattr(roster, "agents", None)
    if agents is not None:
        return list(agents)
    return list(roster)


def build_routes(
    local_served: Iterable[str],
    peer_rosters: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Build the static queue-routing table (agent → owning peer; LEG-095 Phase 3).

    Local-first: an agent the node serves itself is *never* tabled. Then the
    first offering peer in configured order wins — the same order the
    ``StepResolver`` walks. The table routes by name only (rosters carry no
    version); interface skew is enforced owner-side instead — every routed
    deposit stamps the author's ``schema_version`` and the owner refuses a
    mismatch loudly (``StepResolver`` stays the pure, tested decision unit
    for work-item-style delegation). A name no peer offers is a table miss →
    local (today's behavior; the flow layer's own checks still apply).
    """
    owned: dict[str, str] = {}
    local = frozenset(local_served)
    for peer_id, roster in (peer_rosters or {}).items():
        for agent in _roster_names(roster):
            if agent in local or agent in owned:
                continue
            owned[agent] = peer_id
    return owned


async def fetch_peer_catalogs(
    peers: Mapping[str, str],
    token: str | None,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, PeerRoster]:
    """Fetch each configured peer's LEG-090 ``GET /catalog`` roster over the L1.

    Fail-fast (rule 9): a peer that is unreachable, refuses, or returns an
    unparseable roster is a visible ``RecoverableError`` naming the peer and its
    URL — a silently degraded federation would strand cross-node deposits.
    An injected ``client`` (ASGI upstream, respx, or a live transport) is used
    as-is; otherwise a short-lived client is opened and closed around the call.
    """
    entries: dict[str, PeerRoster] = {}
    owned_client = client is None
    active = client if client is not None else httpx.AsyncClient()
    try:
        for peer_id, base_url in peers.items():
            url = f"{base_url.rstrip('/')}/catalog"
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            try:
                response = await active.get(url, headers=headers)
            except httpx.HTTPError as exc:
                logger.warning("federation catalog fetch failed peer=%s url=%s", peer_id, url)
                raise RecoverableError(
                    f"federation catalog fetch failed peer={peer_id} url={base_url} "
                    f"reason={exc}"
                ) from exc
            if response.status_code != 200:
                logger.warning(
                    "federation catalog fetch refused peer=%s url=%s code=%s",
                    peer_id,
                    url,
                    response.status_code,
                )
                raise RecoverableError(
                    f"federation catalog fetch refused peer={peer_id} url={base_url} "
                    f"code={response.status_code}"
                )
            body = response.json()
            entries[peer_id] = PeerRoster(
                agents=[
                    PeerCatalogEntry(
                        agent=item["agent"], input_as=item.get("input_as")
                    )
                    for item in body.get("agents", ())
                ]
            )
            missing = [e.agent for e in entries[peer_id].agents if e.input_as is None]
            if missing:
                logger.warning(
                    "federation catalog rosters lack input_as peer=%s agents=%s",
                    peer_id,
                    ",".join(missing),
                )
                raise RecoverableError(
                    f"federation catalog peer={peer_id} url={base_url} offers agents "
                    f"without an input_as: {missing}"
                )
        logger.info("federation rosters fetched peers=%s", ",".join(sorted(entries)))
        return entries
    finally:
        if owned_client:
            await active.aclose()


class NodeDB(AsyncBeaverDB):
    """A transparent ``AsyncBeaverDB`` proxy routing deposits by queue name.

    The boot hands the agents **this** handle — their single ``db``, never the
    raw database (LEG-095 Phase 3 §A). It subclasses ``AsyncBeaverDB`` without
    opening its own connection: the instance shares the raw db's connection
    state (no second connection; the proxy never owns the lifecycle — only the
    boot closes the raw db), so ``isinstance(node_db, AsyncBeaverDB)`` holds
    and materialization signatures are untouched.

    Lifecycle hazard, documented not hidden (LEG-103 Slice 5e): attribute
    delegation means a holder *could* reach the raw ``close`` through the
    proxy — agents must never do that; shutdown goes through ``NodeDB.aclose``
    (deposit client only) and then the boot-owned raw ``close``. An injected
    HTTP client is never closed here: its lifecycle belongs to its caller.


    ``queue(name)`` applies pure name rules (no I/O at route time):

    - ``legio:queue:result:<task_id>`` → the author origin is parsed from the
      task id: self → local; a configured peer → that peer; a malformed/legacy
      shape → local (back-compat); a well-formed but unknown origin → visible
      ``RecoverableError`` (a genuine routing failure must never strand
      silently, rule 9).
    - ``legio:queue:<agent>`` / ``legio:queue:gather:<agent>`` → the static
      table (agent → owning peer; local-first: locally served agents are never
      tabled). A table miss → local.
    - Anything else (``node_ops``, ``state_report``, …) → local, always.

    ``dict()``/``lock()`` and every other member delegate to the raw db
    (node-local by construction); only ``queue`` is name-routed.
    """

    def __init__(
        self,
        db: AsyncBeaverDB,
        *,
        node_id: str,
        routes: Mapping[str, str] | None = None,
        peers: Mapping[str, str] | None = None,
        federation_token: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._db = db
        self._node_id = node_id
        #: agent name → owning peer (the boot-built static table).
        self.routes = dict(routes or {})
        self._peers = dict(peers or {})
        self._token = federation_token
        self._client = client
        self._owns_client = client is None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)

    def ensure_client(self) -> httpx.AsyncClient:
        """The shared remote-deposit client, created lazily and owned here."""
        if self._client is None:
            self._client = httpx.AsyncClient()
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        """Close the lazily created deposit client, if the proxy owns one.

        An injected client stays open: its lifecycle belongs to the caller.
        The raw database is never touched here — only the boot closes it.
        """
        client, self._client = self._client, None
        if client is not None and self._owns_client:
            self._owns_client = False
            await client.aclose()
            logger.info("federation proxy client closed node=%s", self._node_id)

    def queue(self, name: str, model: type[object] | None = None) -> AsyncBeaverQueue[Any]:
        """Route a beaver queue by name: local beaver or a remote ``put`` shim."""
        owner = self._queue_owner(name)
        if owner is None:
            return self._db.queue(name, model=model)
        return RemoteQueue(self, name, owner)  # type: ignore[return-value]

    def dict(
        self, name: str, model: type[Any] | None = None, secret: str | None = None
    ) -> AsyncBeaverDict[Any]:
        """Node-local dict scopes: delegate straight to the raw db."""
        return self._db.dict(name, model=model, secret=secret)

    def _queue_owner(self, name: str) -> str | None:
        """The owning peer of a full beaver queue name, or ``None`` for local."""
        if not name.startswith(QUEUE_NAMESPACE):
            return None
        relative = name[len(QUEUE_NAMESPACE):]
        if relative.startswith(_RESULT_PREFIX):
            inner = relative[len(_RESULT_PREFIX):]
            match = _TASK_ORIGIN_RE.match(inner)
            if match is None:
                return None  # malformed/legacy → local (back-compat)
            origin = match.group(1)
            if origin == self._node_id:
                return None
            if origin not in self._peers:
                raise RecoverableError(
                    f"result queue {name!r} carries an unknown author origin "
                    f"{origin!r} (not this node, not a configured peer)"
                )
            return origin
        if relative.startswith(_GATHER_PREFIX):
            return self.routes.get(relative[len(_GATHER_PREFIX):])
        return self.routes.get(relative)


class RemoteQueue:
    """The read-less shim at the foreign end of a routed queue name.

    The cross-node leg is pure transport: ``put`` deposits onto the owning
    peer's federation-only ``POST /deposits`` with the L1 bearer and lets the
    owner perform its own local ``put`` — no direct remote write ever exists.
    Read verbs (``get``/``peek``/``count``) on a foreign queue are a visible
    violation: they raise ``RecoverableError``, never serve. Transport failures
    (unreachable owner) and owner refusals (a gate denial, an unknown agent)
    raise the same ``RecoverableError`` carrying peer + code — the flow's
    ``_deliver`` then fails the step loudly through the normal error path
    (never silent, rule 9).
    """

    def __init__(self, proxy: NodeDB, queue_name: str, owner: str) -> None:
        self._proxy = proxy
        self._queue = queue_name
        self._owner = owner

    def _deposit_url(self) -> str:
        base = self._proxy._peers.get(self._owner)
        if base is None:
            raise RecoverableError(
                f"remote deposit queue={self._queue} peer={self._owner} "
                "(no endpoint for this peer)"
            )
        return f"{base.rstrip('/')}/deposits"

    def _client(self) -> httpx.AsyncClient:
        return self._proxy.ensure_client()

    def _read_denied(self) -> RecoverableError:
        return RecoverableError(
            f"foreign queue {self._queue!r} is owned by peer {self._owner!r}; "
            "reads are local-only (the cross-node leg is transport)"
        )

    async def put(self, data: dict[str, Any], priority: float) -> None:
        """Deposit onto the owner's queue via ``POST /deposits`` (L1 bearer).

        The current flow ``schema_version`` is stamped on the wire so the
        owner can refuse a stale peer loudly instead of executing it
        silently (LEG-091 version gate, enforced owner-side)."""
        if not isinstance(data, dict):
            raise RecoverableError(
                f"remote deposit queue={self._queue} peer={self._owner} "
                "refuses a non-dict item (flow messages are objects)"
            )
        url = self._deposit_url()
        token = self._proxy._token
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = await self._client().post(
                url,
                json={
                    "queue": self._queue,
                    "item": data,
                    "priority": priority,
                    "schema_version": SCHEMA_VERSION,
                },
                headers=headers,
            )
        except httpx.HTTPError as exc:
            logger.warning("federation deposit remote failed peer=%s queue=%s", self._owner, self._queue)
            raise RecoverableError(
                f"remote deposit failed peer={self._owner} queue={self._queue} reason={exc}"
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            body_code: str | None = None
            try:
                body_code = response.json().get("code")
            except ValueError:
                body_code = None
            code = body_code or str(response.status_code)
            logger.warning(
                "federation deposit remote refused peer=%s queue=%s code=%s",
                self._owner,
                self._queue,
                code,
            )
            raise RecoverableError(
                f"remote deposit refused peer={self._owner} queue={self._queue} code={code}"
            )
        logger.info("federation deposit remote queue=%s peer=%s", self._queue, self._owner)

    async def get(self, block: bool = True, timeout: float | None = None) -> Any:
        raise self._read_denied()

    async def peek(self) -> Any:
        raise self._read_denied()

    async def count(self) -> int:
        raise self._read_denied()


__all__ = [
    "AgentInterface",
    "InterfaceMismatchError",
    "Local",
    "NodeDB",
    "PeerCatalogEntry",
    "PeerRoster",
    "Remote",
    "RemoteQueue",
    "StepResolver",
    "UnresolvableAgentError",
    "build_routes",
    "fetch_peer_catalogs",
    "roster_steps",
]