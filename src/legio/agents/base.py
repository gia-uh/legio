"""`legio.agents.base` — the uniform per-step runner (LEG-023) over native beaver.

Every agent — atomic (tool/linguistic), composite and root — is a uniform
``run()`` unit. ``AgentBase`` provides that loop once: it pops a work item from
the agent's native beaver queue (``db.queue``), dispatches it to the step's job
(a subclass ``_handle``), applies the ``monitor`` hook, and routes the outcome
by position (Schema 2): advance to the next class of this level as an
``ExecutionRequestMessage``, or — at the end of the level — deposit an
``ExecutionResultMessage`` to the level's ``end_of_level_queue`` (the submit's
final-result queue at level 1, AGENT_LIFECYCLE §12.1). The route and the
destination travel inside the message itself: routing is always derived from
``level_route``/``current_index``/``end_of_level_queue``, never from
caller-owned knowledge, and the step's produced payload travels in the single
``payload`` container (Schema 2).

The actual steps (linguistic, tool, composite) plug in via ``_handle``, which
returns the new payload to route; ``ToolAgent`` (LEG-022) is one such subclass.
How an agent **builds** its output is its own implementation: ``_handle``
produces the step's info and hands it to the re-implementable seam
``build_output_as``, which constructs the ``{output_as: value}`` payload — the
engine never guesses that shape (per type, basic models exist as overridable
defaults; a pattern may inherit and build its own for any agent type).

Failures are never silent (AGENTS.md rule 9): a raised step error is surfaced
as an ``error`` result. The declared contracts are verified uniformly here on
both edges — **superset**: the incoming data must contain the ``input_schema``
and the built payload the ``output_schema`` (extras are irrelevant), for every
agent, atomic or composite (missing data or wrong strict types raise
``ContractError``). There is no lease, no retry and no re-queue in the
dispatch: the agent is a stateless poller (AGENTS.md rule 8) — nothing sleeps,
nothing is locked, and the item is simply consumed once.

No invented substrate layer exists: the agent speaks beaver natively,
exactly as castor's Manager holds a ``db`` and calls ``db.dict``/``db.queue``
directly.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from beaver import AsyncBeaverDB
from pydantic import BaseModel, ValidationError

from legio.flow import ExecutionRequestMessage, ExecutionResultMessage, MessageType
from legio.naming import queue_key
from legio.patterns.compile import compile_schema

logger = logging.getLogger(__name__)

Monitor = Callable[[str, str, str], Awaitable[None]]

_EVENT_START = "start"
_EVENT_STEP_DONE = "step_done"
_EVENT_STEP_ERROR = "step_error"
_EVENT_IDLE = "idle"


class ContractError(RuntimeError):
    """A declared ``input_schema``/``output_schema`` contact was violated.

    Raised by the uniform runner when the data at an agent's edge does not
    *contain* the declared contract (superset): a required declared property is
    missing or has the wrong (strict) type. It is surfaced like any step error —
    a visible ``error`` result, never silent (AGENTS.md rule 9).
    """


class AgentBase:
    """The uniform run loop every agent implements (LEG-023) on native beaver."""

    # Read alias owned by the step, set by subclasses (LEG-022/030/040). Defaults
    # to "" — the agent then works on the whole payload. Kept as a plain class
    # attribute so a subclass assignment in its own __init__ is never clobbered.
    _input_as: str = ""

    def __init__(
        self,
        *,
        agent_id: str,
        db: AsyncBeaverDB,
        output_as: str = "",
        input_schema: Mapping[str, Any] | None = None,
        output_schema: Mapping[str, Any] | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._db = db
        self._output_as = output_as
        # The agent's declared contracts, compiled once to strict pydantic
        # models (superset check, §12.1): None when the pattern declares no
        # schema (plain text default — nothing to verify).
        self._input_contract: type[BaseModel] | None = (
            compile_schema(input_schema) if input_schema else None
        )
        self._output_contract: type[BaseModel] | None = (
            compile_schema(output_schema) if output_schema else None
        )
        self._queue = db.queue(queue_key(agent_id))
        self._monitor: Monitor | None = None

    def set_hooks(self, *, monitor: Monitor | None = None) -> None:
        """Register the optional ``monitor`` observability hook."""
        self._monitor = monitor

    @property
    def agent_id(self) -> str:
        """The agent's stable identity (its queue/namespace name)."""
        return self._agent_id

    async def run(self, *, max_steps: int = 100) -> int:
        """Poll the queue until idle or ``max_steps`` reached; return steps done.

        Bounded so a misbehaving step can never starve the agent into an
        infinite busy loop.
        """
        steps = 0
        while steps < max_steps:
            handled = await self.process_next()
            if not handled:
                break
            steps += 1
        return steps

    async def process_next(self) -> bool:
        """Consume at most one work item; return whether one was handled.

        The native beaver queue ``get(block=False)`` pops destructively: there
        is no lease, no ``next_run_at`` gate and no re-queue — the item is
        consumed once and routed. Idle returns ``False`` (rule 8, polling only).
        """
        try:
            qitem = await self._queue.get(block=False)
        except IndexError:
            logger.debug("agent idle agent=%s", self._agent_id)
            return False

        item = qitem.data
        try:
            request = ExecutionRequestMessage.model_validate(dict(item))
            logger.info(
                "agent run agent=%s task=%s level=%s index=%s",
                self._agent_id,
                request.task_id,
                request.level,
                request.current_index,
            )
            await self._emit(_EVENT_START, request)
            await self._run_guarded(request)
        except Exception:
            logger.exception(
                "agent crashed agent=%s task=%s",
                self._agent_id,
                item.get("task_id", "?"),
            )
            raise
        logger.debug("agent done agent=%s", self._agent_id)
        return True

    async def _run_guarded(self, request: ExecutionRequestMessage) -> None:
        """Run the step job and route its outcome, or route a raised failure.

        The uniform contract check wraps every step on both edges (rule 12):
        the incoming data is verified against the agent's ``input_schema``
        before the job runs, and the built payload against its ``output_schema``
        before it is routed — superset on both edges, for every agent, atomic or
        composite. A violation raises ``ContractError`` and is routed as a
        visible ``error`` result, never silent.
        """
        new_payload: dict[str, Any] | None = None
        try:
            self._verify_input_contract(request)
            new_payload = await self._handle(request)
            if new_payload is not None:
                self._verify_output_contract(new_payload)
        except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
            error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "agent step error agent=%s task=%s error=%s",
                self._agent_id,
                request.task_id,
                error,
            )
            await self._emit(_EVENT_STEP_ERROR, request)
            await self._route_outcome(request, {"error": error})
            return
        if new_payload is not None:
            await self._route_outcome(request, new_payload)
            await self._emit(_EVENT_STEP_DONE, request)

    def _scoped_input(self, request: ExecutionRequestMessage) -> Any:
        """The step's input data: the under-alias payload, or the whole payload."""
        alias = self._input_as
        if alias and alias in request.payload:
            return request.payload[alias]
        return request.payload

    def _verify_input_contract(self, request: ExecutionRequestMessage) -> None:
        """Superset-check the incoming data against the agent's ``input_schema``.

        The payload must *contain* the declared input data — missing declared
        properties or wrong strict types reject the step with a raised
        ``ContractError``; undeclared extras are irrelevant and pass through.
        No contract declared → no check. A fan-in ``EXECUTION_RESULT`` (a
        returned branch state, composite-internal) is not an entry — the input
        contract applies to entry requests only.
        """
        model = self._input_contract
        if model is None:
            return
        if request.message_type is MessageType.EXECUTION_RESULT:
            return
        try:
            model.model_validate(self._scoped_input(request))
        except ValidationError as exc:
            raise ContractError(
                f"input contract rejected agent={self._agent_id} task={request.task_id} "
                f"alias={self._input_as!r} problems={self._summarize_errors(exc)}"
            ) from exc

    def _verify_output_contract(self, payload: dict[str, Any]) -> None:
        """Superset-check the built payload against the agent's ``output_schema``.

        Runs after construction, before the payload is routed: the value under
        the agent's ``output_as`` must contain everything the schema declared
        (missing/typed-wrong → ``ContractError``; extras pass through). A
        payload without the agent's ``output_as`` (an ``error`` result) is
        passed through unverified — never swallowed (AGENTS.md rule 9).
        """
        model = self._output_contract
        if model is None:
            return
        alias = self._output_as
        if not alias or alias not in payload:
            return
        try:
            model.model_validate(payload[alias])
        except ValidationError as exc:
            raise ContractError(
                f"output contract rejected agent={self._agent_id} alias={alias!r} "
                f"problems={self._summarize_errors(exc)}"
            ) from exc

    @staticmethod
    def _summarize_errors(exc: ValidationError) -> str:
        """Compact `loc:type` list (never the offending values — the error is
        routed inside a message payload that must stay small)."""
        return ", ".join(
            f"{'.'.join(str(part) for part in error['loc'])}:{error['type']}"
            for error in exc.errors()
        )

    async def _emit(self, event: str, request: ExecutionRequestMessage | None) -> None:
        if self._monitor is None:
            return
        task_id = request.task_id if request is not None else "?"
        await self._monitor(self._agent_id, task_id, event)

    async def _route_outcome(
        self, request: ExecutionRequestMessage, payload: dict[str, Any]
    ) -> None:
        """Route the produced payload to the next class or the level closer.

        Routing is Schema 2 position-based: while ``current_index + 1`` is inside
        ``level_route`` the outcome advances to ``level_route[current_index + 1]``
        as an ``ExecutionRequestMessage``, **re-keying the produced value** (the
        agent's ``output_as``) under the next step's ``input_as`` (AGENT_LIFECYCLE
        §12.1); at the end of the level it is deposited to ``end_of_level_queue``
        as an ``ExecutionResultMessage`` (the submit's final-result queue at
        ``level == 1``). The destination is never caller-owned — everything rides
        in the message (ARCHITECTURE §0/§3).
        """
        next_index = request.current_index + 1
        if next_index < len(request.level_route):
            next_step = request.level_route[next_index]
            next_class, next_input_as = next_step
            logger.info(
                "agent advance agent=%s task=%s level=%s to=%s index=%s",
                self._agent_id,
                request.task_id,
                request.level,
                next_class,
                next_index,
            )
            next_request = ExecutionRequestMessage(
                level_route=request.level_route,
                current_index=next_index,
                end_of_level_queue=request.end_of_level_queue,
                level=request.level,
                launcher_class=request.launcher_class,
                task_id=request.task_id,
                branch_id=request.branch_id,
                payload=self._rekey(payload, next_input_as),
            )
            await self._deliver(next_class, next_request.model_dump(mode="json"))
            return

        if request.level == 1:
            logger.info(
                "agent finish agent=%s task=%s to_end_queue=%s",
                self._agent_id,
                request.task_id,
                request.end_of_level_queue,
            )
        else:
            logger.info(
                "agent branch close agent=%s task=%s level=%s to=%s",
                self._agent_id,
                request.task_id,
                request.level,
                request.end_of_level_queue,
            )
        result = ExecutionResultMessage(
            level_route=request.level_route,
            current_index=request.current_index,
            end_of_level_queue=request.end_of_level_queue,
            level=request.level,
            launcher_class=request.launcher_class,
            task_id=request.task_id,
            branch_id=request.branch_id,
            payload=payload,
        )
        await self._deliver(request.end_of_level_queue, result.model_dump(mode="json"))

    async def _deliver(self, target: str, item: dict[str, Any]) -> None:
        """Put the message onto a queue by name (class or level closer)."""
        await self._db.queue(queue_key(target)).put(dict(item), priority=0.0)

    def _rekey(self, payload: dict[str, Any], next_input_as: str) -> dict[str, Any]:
        """Re-key the produced payload under the next step's ``input_as``.

        The producer builds ``{output_as: <value>}`` (construction, not
        accumulation); whoever hands the task to the next agent re-keys that value
        under the next agent's ``input_as`` (AGENT_LIFECYCLE §12.1). A payload
        that does not carry the producer's ``output_as`` (an ``error`` result) is
        passed through unchanged, never swallowed (AGENTS.md rule 9).
        """
        if self._output_as and self._output_as in payload:
            return {next_input_as: payload[self._output_as]}
        return dict(payload)

    async def _handle(
        self, request: ExecutionRequestMessage
    ) -> dict[str, Any] | None:  # pragma: no cover - abstract
        """Execute the step's job; return the new payload (or None to drop).

        Implemented by concrete agents. A returned dict is routed by position by
        ``_route_outcome``; a raised exception is surfaced as an error result by
        ``_run_guarded``. The step's produced *info* is handed to
        ``build_output_as``, which constructs the payload — the seam every agent
        re-implements.
        """
        raise NotImplementedError

    async def build_output_as(self, info: Any) -> dict[str, Any]:  # pragma: no cover - abstract
        """Construct the agent's new payload from its produced ``info`` (seam).

        Exact output built from the produced info is the agent's own
        implementation: ``output_as`` is the agent's write alias and the way it
        composes the value under it is what a pattern re-implements (AGENT_LIFECYCLE
        §12.1 construction). ``info`` is whatever the type's runner yields
        (a tool's raw output, a linguistic record's dump, a composite's grouped
        branch slots); the return value is always ``{output_as: <value>}``. Basic
        models exist per type in each subclass (overridable); the engine never
        supplies a generic shape.
        """
        raise NotImplementedError


__all__ = ["AgentBase", "ContractError"]
