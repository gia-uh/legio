"""`legio.agents.tool_agent` — the ToolAgent runner (LEG-022, over LEG-023).

The ToolAgent executes a `kind: tool` agent step. It receives the incoming
payload from the request's single `payload` container (Schema 2), resolves the
terse `parameters` (`{arg: dotted.path | literal}`) against it, loads the bound
`tool: <name>` from `available_tools` (Schema 3), invokes it with the resolved
kwargs (sync tools run off the loop, async tools are awaited, both under the
declared per-call `timeout`; `retries` stays 0), validates the call against
the tool's signature at execution time,
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

import asyncio
import inspect
import logging
import math
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
            # Enforce the declared Schema 3 policy: retries stay 0 (the engine
            # never retries a step), timeout bounds one call for both sync
            # (run off the loop) and async tools.
            timeout, retries = self._tool_policy()
            if retries is not None and retries != 0:
                raise ValueError(
                    f"tool {self._tool_name!r} declares retries={retries!r}: "
                    "only retries=0 is supported (steps are never retried)"
                )
            raw_output = await self._invoke_tool(tool, resolved_kwargs, timeout)
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

    def _tool_policy(self) -> tuple[float | None, int | None]:
        """Read the tool's declared ``(timeout, retries)`` policy.

        ``timeout`` bounds one call in seconds (``None`` = unbounded);
        ``retries`` must stay ``0``/``None`` — the engine never retries.
        The file path is validated at load (`ToolPolicy`); this guards the
        direct-registry path just as loudly (rule 9).
        """
        declaration = self._available_tools.get_declaration(self._tool_name)
        policy = declaration.get("policy") or {}
        timeout = policy.get("timeout")
        retries = policy.get("retries")
        if isinstance(timeout, bool):
            raise ValueError(  # noqa: TRY004 - value rejection, naming the tool
                f"tool {self._tool_name!r} declares timeout={timeout!r}: "
                "policy.timeout must be a finite number of seconds > 0, never a boolean"
            )
        try:
            timeout_value = float(timeout) if timeout is not None else None
        except (TypeError, ValueError):
            timeout_value = None
            invalid = True
        else:
            invalid = timeout_value is not None and (
                not math.isfinite(timeout_value) or timeout_value <= 0
            )
        if invalid:
            raise ValueError(
                f"tool {self._tool_name!r} declares timeout={timeout!r}: "
                "policy.timeout must be a finite number of seconds > 0"
            )
        return (timeout_value, retries)

    async def _invoke_tool(self, tool: Any, kwargs: dict[str, Any], timeout: float | None) -> Any:
        """Invoke a sync or async tool under the policy timeout.

        Sync tools always run off the event loop (`asyncio.to_thread`) so one
        slow call can never stall every pump — whether or not a timeout is
        declared. Whatever the call returns, an awaitable is awaited (async
        callables, sync callables handing back a coroutine, chained
        awaitables). Generator shapes are not values and fail loudly instead
        of leaking an unconsumed object into the payload.
        """
        if inspect.isasyncgenfunction(tool):
            raise TypeError(
                f"tool {self._tool_name!r} is an async generator: "
                "tools must be sync or async callables returning a value"
            )
        if inspect.isgeneratorfunction(tool):
            raise TypeError(
                f"tool {self._tool_name!r} is a sync generator: "
                "tools must be sync or async callables returning a value"
            )
        if inspect.iscoroutinefunction(tool):
            return await self._bound(self._await_async_callable(tool, kwargs), timeout)
        return await self._bound(self._call_sync_shape(tool, kwargs), timeout)

    async def _bound(self, awaitable: Any, timeout: float | None) -> Any:
        """Await one tool-call awaitable under the declared timeout, if any."""
        if timeout is None:
            return await awaitable
        return await asyncio.wait_for(awaitable, timeout)

    async def _await_async_callable(self, tool: Any, kwargs: dict[str, Any]) -> Any:
        """Await a known-async tool and any awaitable it hands back."""
        result = await tool(**kwargs)
        while inspect.isawaitable(result):
            result = await result
        return self._ensure_value_shape(result)

    async def _call_sync_shape(self, tool: Any, kwargs: dict[str, Any]) -> Any:
        """Run a sync-shape tool off the loop, then await any awaitable back."""
        result = await asyncio.to_thread(tool, **kwargs)
        while inspect.isawaitable(result):
            result = await result
        return self._ensure_value_shape(result)

    def _ensure_value_shape(self, result: Any) -> Any:
        """Reject generator objects: they are not tool values (rule 9)."""
        if inspect.isasyncgen(result) or inspect.isgenerator(result):
            raise TypeError(
                f"tool {self._tool_name!r} produced a generator: "
                "tools must return a value, never an iterator"
            )
        return result

    async def build_output_as(self, info: Any) -> dict[str, Any]:
        """Basic tool-output model: the tool's raw output is the value.

        ``info`` is what the tool call returned. Override this seam (inherit
        ``ToolAgent``) to transform the raw output into the pattern's own
        declared value before it is wrapped under ``output_as`` (§12.1).
        """
        return build_payload(info, output_as=self._output_as)


__all__ = ["ToolAgent"]
