"""`legio.federation` — the federated step resolver (LEG-091, R-9).

The author resolves each step's required agent **before deposit**: local when
the node serves it, remote when a configured peer's catalog offers it with a
matching ``schema_version``, error otherwise. The resolver is a *pure
decision* — no beaver, no HTTP, no deposits. The client layer (LEG-092) fetches
peer rosters (LEG-090 ``GET /catalog`` responses) and deposits work items; the
acceptor's agents therefore never receive work for a pattern they do not serve
(ARCH §9).

Local-first framing (ARCH §9, maintainer session 85q): federation transports
*work*, never lifecycle. A node cannot control another node's agents; the
resolver only decides whether a step executes at home or on a peer.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from legio.errors import LegioError
from legio.fed import AgentInterface
from legio.flow import SCHEMA_VERSION

logger = logging.getLogger(__name__)


class InterfaceMismatchError(LegioError):
    """A peer advertises the agent with an incompatible ``schema_version``.

    Never silently skipped (rule 9): an offered-but-incompatible agent is a
    visible author-time failure, not a hop to the next peer.
    """


class UnresolvableAgentError(LegioError):
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


__all__ = [
    "InterfaceMismatchError",
    "Local",
    "Remote",
    "StepResolver",
    "UnresolvableAgentError",
]