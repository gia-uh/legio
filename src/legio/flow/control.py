"""`legio.flow.control` — the authenticated control channel (LEG-082).

The only way to talk to an agent is its queue. Lifecycle orders arrive as
signed ``ControlMessage``s on the agent's class queue — minted **only** by the
node's Runtime (never by an agent), verified by a pure, verify-only handle the
agent holds at materialization. An agent can *validate* authenticity; it can
never forge a message (the key is in-process, per-boot, never persisted, and
rotating it per boot means a signature from a previous boot never validates
after restart).

Integrity model: the ``signature`` is an HMAC-SHA256 over the canonical JSON of
every field except the signature itself. Verification is pure — no state, no
timers, no process coupling. ``seq`` is monotonic per instance: the agent
rejects ``seq <= last-seen`` (anti-replay within a boot; the per-boot key closes
the across-restart window). Control deposits use ``CONTROL_PRIORITY`` (lower
than the work ``0.0``, beaver ``ORDER BY priority ASC``) so a control message
jumps the FIFO work at the agent's next dispatch.

Domain-free (rule 7): nothing here knows any consumer domain — only the two
lifecycle intents of the node's own Runtime and the verification algebra.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .messages import SCHEMA_VERSION

# Control messages are deposited with a lower priority than work (beaver orders
# by ``priority ASC``; work stays ``0.0``), so control jumps the FIFO.
CONTROL_PRIORITY = -1.0
CONTROL_MESSAGE_TYPE = "control"

_CONTROL_KEY_DOMAIN = b"legio:node-control-key:v1"


class ControlAction(str, Enum):
    """The only lifecycle orders the node's Runtime ever sends."""

    ENABLE = "enable"
    DISABLE = "disable"
    TERMINATE_WITH_DRAIN = "terminate_with_drain"


class ControlOrigin(str, Enum):
    """Who decided the order: a human operator or a node policy."""

    OPERATOR = "operator"
    AUTOMATIC = "automatic"


def derive_control_key(secret_material: str | bytes) -> bytes:
    """Derive the per-boot, in-process HMAC key from a boot secret.

    The input is domain-separated and the result is a fixed 32-byte key. The
    key lives only in-process for the boot's lifetime — never persisted, never
    logged (rule 11 does not apply to secrets; it is documentation-dead).
    """
    material = secret_material.encode() if isinstance(secret_material, str) else secret_material
    return hmac.new(_CONTROL_KEY_DOMAIN, hashlib.sha256(material).digest(), hashlib.sha256).digest()


class ControlMessage(BaseModel):
    """A signed lifecycle order addressed to one instance (LEG-082)."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_assignment=True,
    )

    schema_version: int = Field(default=SCHEMA_VERSION, frozen=True)
    message_type: Literal["control"] = CONTROL_MESSAGE_TYPE
    target_instance: str
    action: ControlAction
    origin: ControlOrigin
    seq: int = Field(ge=1, frozen=True)
    signature: str = Field(default="", frozen=True)


def _canonical(message: ControlMessage) -> bytes:
    """Deterministic byte form of every signed field (excluding the signature)."""
    payload: dict[str, Any] = {
        "schema_version": message.schema_version,
        "message_type": message.message_type,
        "target_instance": message.target_instance,
        "action": message.action.value,
        "origin": message.origin.value,
        "seq": message.seq,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return encoded


def sign_control(
    key: bytes,
    *,
    target_instance: str,
    action: ControlAction,
    seq: int,
    origin: ControlOrigin = ControlOrigin.OPERATOR,
) -> ControlMessage:
    """Mint a signed control message (the Runtime's minting privilege only)."""
    unsigned = ControlMessage(
        target_instance=target_instance,
        action=action,
        origin=origin,
        seq=seq,
    )
    signature = hmac.new(key, _canonical(unsigned), hashlib.sha256).hexdigest()
    return ControlMessage(
        target_instance=target_instance,
        action=action,
        origin=origin,
        seq=seq,
        signature=signature,
    )


class ControlVerifier:
    """The pure, verify-only handle an agent holds (cannot mint, LEG-082).

    Injected at materialization (LEG-087 wires the injection). Verification is
    stateless: recompute the HMAC over the canonical fields and compare
    constant-time. It cannot produce a valid signature for any message — it has
    no minting path.
    """

    def __init__(self, key: bytes) -> None:
        self._key = key

    def verify(self, message: ControlMessage) -> bool:
        expected = hmac.new(self._key, _canonical(message), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, message.signature)


__all__ = [
    "CONTROL_MESSAGE_TYPE",
    "CONTROL_PRIORITY",
    "ControlAction",
    "ControlMessage",
    "ControlOrigin",
    "ControlVerifier",
    "derive_control_key",
    "sign_control",
]