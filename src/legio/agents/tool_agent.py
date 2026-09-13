"""`legio.agents.tool_agent` — the ToolAgent runner (LEG-022, over LEG-023).

The ToolAgent executes a `kind: tool` agent step. It receives the incoming
payload from the request's single `payload` container (Schema 2), resolves the
terse `parameters` (`{arg: dotted.path | literal}`) against it, loads the bound
`tool: <name>` from `available_tools` (Schema 3), invokes it with the resolved
kwargs, validates the call against the tool's signature at execution time,
and builds the new payload with `build_payload` (AGENT_LIFECYCLE §12.1: the
state travels in the messages — nothing staged out-of-message). The base routes
by position. How the tool's raw output becomes the agent's `output_as` value is
the agent's own model: the runner hands the raw output to the re-implementable
seam `build_output_as`, whose basic tool model wraps it as-is — a pattern may
inherit `ToolAgent` and re-implement it for a custom output shape.

Schema/signature failures on either edge are never silent: an error result is
deposited instead (see AGENTS.md rule 9).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from legio.agents.base import AgentBase
from legio.flow import ControlVerifier, ExecutionRequestMessage, build_payload
from legio.tools import AvailableToolsRegistry, resolve_parameters, validate_callable_signature

logger = logging.getLogger(__name__)


class ToolAgent(AgentBase):
    """Runs a single tool step of a route against a Schema 3 tool."""

    def __init__(
        self,
        *,
        agent_id: str,
        db: Any,
        available_tools: AvailableToolsRegistry,
        tool_name: str,
        parameters: Mapping[str, Any],
        input_as: str = "",
        output_as: str = "",
        input_schema: Mapping[str, Any] | None = None,
        output_schema: Mapping[str, Any] | None = None,
        control_verifier: ControlVerifier | None = None,
    ) -> None:
        super().__init__(
            agent_id=agent_id,
            db=db,
            output_as=output_as,
            input_schema=input_schema,
            output_schema=output_schema,
            control_verifier=control_verifier,
        )
        self._available_tools = available_tools
        self._tool_name = tool_name
        self._parameters = dict(parameters)
        self._input_as = input_as

    async def _handle(self, request: ExecutionRequestMessage) -> dict[str, Any]:
        error: str | None = None
        try:
            # Resolve terse parameters against the incoming payload (explicit
            # `{input_as}.{key}` dotted paths, §4.12 — no implicit resolution).
            resolved_kwargs = resolve_parameters(self._parameters, request.payload)
            # Load the tool from available_tools (dotted path)
            tool = self._available_tools.load_tool(self._tool_name)
            # Validate against tool's signature at execution time
            validate_callable_signature(tool, resolved_kwargs)
            # Invoke
            raw_output = tool(**resolved_kwargs)
            logger.debug(
                "tool executed ok agent=%s task=%s tool=%s",
                self._agent_id,
                request.task_id,
                self._tool_name,
            )
            # Build via the re-implementable output seam (construction under
            # output_as; re-keying at handoff).
            return await self.build_output_as(raw_output)
        except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
            logger.warning(
                "tool execution failure agent=%s task=%s tool=%s error=%s",
                self._agent_id,
                request.task_id,
                self._tool_name,
                f"{type(exc).__name__}: {exc}",
            )
            error = f"{type(exc).__name__}: {exc}"

        if error is not None:
            return {"error": error}

        # Should not reach here
        return {"error": "tool produced no output"}

    async def build_output_as(self, info: Any) -> dict[str, Any]:
        """Basic tool-output model: the tool's raw output is the value.

        ``info`` is what the tool call returned. Override this seam (inherit
        ``ToolAgent``) to transform the raw output into the pattern's own
        declared value before it is wrapped under ``output_as`` (§12.1).
        """
        return build_payload(info, output_as=self._output_as)


__all__ = ["ToolAgent"]
