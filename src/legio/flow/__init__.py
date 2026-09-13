"""`legio.flow` — the FlowToken, the two queue message types (LEG-011) and the
authenticated control channel (LEG-082)."""

from __future__ import annotations

from .control import (
    CONTROL_MESSAGE_TYPE,
    CONTROL_PRIORITY,
    ControlAction,
    ControlMessage,
    ControlOrigin,
    ControlVerifier,
    derive_control_key,
    sign_control,
)
from .messages import (
    SCHEMA_VERSION,
    ExecutionRequestMessage,
    ExecutionResultMessage,
    MessageType,
)
from .payload import build_payload
from .token import FlowToken

__all__ = [
    "CONTROL_MESSAGE_TYPE",
    "CONTROL_PRIORITY",
    "SCHEMA_VERSION",
    "ControlAction",
    "ControlMessage",
    "ControlOrigin",
    "ControlVerifier",
    "ExecutionRequestMessage",
    "ExecutionResultMessage",
    "FlowToken",
    "MessageType",
    "build_payload",
    "derive_control_key",
    "sign_control",
]
