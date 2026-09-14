"""`legio.flow` — the FlowToken, the two queue message types (LEG-011) and the
authenticated control channel (LEG-082)."""

from __future__ import annotations

from .control import (
    CONTROL_MESSAGE_TYPE,
    CONTROL_PRIORITY,
    STATE_REPORT_MESSAGE_TYPE,
    STATE_REPORT_SCOPE,
    AgentStateReport,
    ControlAction,
    ControlMessage,
    ControlOrigin,
    ControlVerifier,
    ReportedState,
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
    "STATE_REPORT_MESSAGE_TYPE",
    "STATE_REPORT_SCOPE",
    "AgentStateReport",
    "ControlAction",
    "ControlMessage",
    "ControlOrigin",
    "ControlVerifier",
    "ExecutionRequestMessage",
    "ExecutionResultMessage",
    "FlowToken",
    "MessageType",
    "ReportedState",
    "build_payload",
    "derive_control_key",
    "sign_control",
]
