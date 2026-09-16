"""`legio.naming` — identifiers and persisted names (LEG-016).

Every identifier (node, agent, tool, task) has a validating contract. There is
no ``client:`` family (Schema 2, addendum AL): root results land on the
submit-seeded final-result queue, addressed by ``result_queue_key``.

The only persisted namespaces legio names directly are the per-agent queue
``legio:queue:<agent_id>``, the per-composite gathering queue
``legio:queue:gather:<agent_id>``, the per-agent final-result queue
``legio:queue:result:<agent>`` (one per served root agent, shared by every
task starting there — LEG-095 Phase 2) and the per-task outbox record
``outbox:<task_id>`` in the Runtime-owned ``outbox`` dict. A composite consumes
**two physical queues** (its class inbox and its gathering — AGENT_LIFECYCLE
§12.2/§12.3); messages are partitioned by queue, never by message type. Flow
results always travel in the message payload. Everything else is beaver's
native naming.
"""

from __future__ import annotations

import logging
import re
from enum import Enum

from legio.errors import InvalidNameError

logger = logging.getLogger(__name__)


class ActivityState(str, Enum):
    """Activity axis of a class or instance (§3/§4.8): ``enabled``/``disabled``.

    Owned here (not by the Registry) so lower layers — agents read gate rows
    — never import the lifecycle layer for vocabulary.
    """

    ENABLED = "enabled"
    DISABLED = "disabled"

QUEUE_NAMESPACE = "legio:queue:"

#: Beaver dict scope holding one outbox record per completed task (LEG-095
#: Phase 2) — the Runtime-owned mirror of collected results, keyed by task id.
OUTBOX_SCOPE = "outbox"


def queue_key(agent_id: str) -> str:
    """Full namespaced beaver queue name for an agent."""
    return f"{QUEUE_NAMESPACE}{agent_id}"


def result_queue_key(agent_name: str) -> str:
    """The queue *name* (relative) of a root agent's final-result queue.

    One queue per served root agent (LEG-095 Phase 2), shared by every task
    starting there — never one queue per submit. The submit seeds this as the
    token's ``end_of_level_queue`` at level 1 and the ``RESULT_DRAIN`` intake
    collects its ``ExecutionResultMessage``s into per-task outbox records,
    which ``status`` reads back. The relative name is resolved to a beaver
    queue via ``queue_key`` when delivering/draining.
    """
    return f"result:{agent_name}"


def outbox_key(task_id: str) -> str:
    """The informational pointer ``status`` reports as ``result_key``.

    Names the task's record in the ``outbox`` dict (``OUTBOX_SCOPE``) once the
    ``RESULT_DRAIN`` intake has collected its result — the address the result
    lives at after collection, never the physical queue.
    """
    return f"outbox:{task_id}"


def gathering_key(agent_id: str) -> str:
    """The queue *name* (relative) of a composite's gathering queue (§12.3).

    A composite consumes two physical queues: its class inbox
    (``queue_key(agent_id)``) and this gathering queue, where its branches
    return their fan-in results via ``end_of_level_queue``. Resolved to a beaver
    queue via ``queue_key`` when delivering/reading, exactly like a
    final-result queue. Results never land on an inbox: partition is by queue.
    """
    return f"gather:{agent_id}"


_NODE_RE = re.compile(r"^[^@]+@[^@]+$")
_AGENT_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_TOOL_RE = re.compile(r"^[a-z][a-z0-9_.]*$")
_TASK_RE = re.compile(r"^[^:]+:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_RESERVED_AGENT_PREFIX = "client:"


def _guard(valid: bool, name: str) -> None:
    if not valid:
        logger.warning("invalid identifier %r rejected", name)
        raise InvalidNameError(f"invalid identifier {name!r}")


def validate_node_id(node_id: str) -> None:
    """A node id is ``<name>@<host>`` with exactly one ``@``."""
    _guard(bool(node_id) and _NODE_RE.match(node_id) is not None, node_id)


def validate_agent_id(agent_id: str) -> None:
    """An agent id is lowercase ``[a-z][a-z0-9_-]*`` and never reserved."""
    _guard(
        bool(agent_id)
        and _AGENT_RE.match(agent_id) is not None
        and not is_reserved_agent(agent_id),
        agent_id,
    )


def validate_tool_id(tool_id: str) -> None:
    """A tool id is consumer-namespaced lowercase ``[a-z][a-z0-9_.]*``."""
    _guard(bool(tool_id) and _TOOL_RE.match(tool_id) is not None, tool_id)


def validate_task_id(task_id: str) -> None:
    """A task id is ``<origin>:<uuid>``."""
    _guard(bool(task_id) and _TASK_RE.match(task_id) is not None, task_id)


def is_reserved_agent(agent_id: str) -> bool:
    """Whether the agent id belongs to the reserved ``client:`` family."""
    return agent_id.startswith(_RESERVED_AGENT_PREFIX)


__all__ = [
    "OUTBOX_SCOPE",
    "QUEUE_NAMESPACE",
    "ActivityState",
    "gathering_key",
    "is_reserved_agent",
    "outbox_key",
    "queue_key",
    "result_queue_key",
    "validate_agent_id",
    "validate_node_id",
    "validate_task_id",
    "validate_tool_id",
]
