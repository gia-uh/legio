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

Every agent polls **its class inbox** (``legio:queue:<class>``); only an
``ExecutionRequestMessage`` — an entry — ever lands there, so the base never
dispatches by message type (partition is by queue, §12.2/§12.3). A composite
additionally owns a second physical queue, its **gathering**
(``legio:queue:gather:<class>``), where its branches return fan-in results; it
overrides ``process_next`` with its two-inlet intake + gated collection cycle
(§12.3).

The **authenticated control channel** (LEG-082) lives on the class inbox as
well: a signed ``ControlMessage`` (minted only by the node's Runtime) is the one
kind of message that also arrives there, and ``standing_loop`` — the instance's
long-lived interior loop — discriminates it from work and honors it **between
dispatches** (§12.2 amended). An agent holds a verifier but never a key: it can
validate, it cannot forge.

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

No invented substrate layer exists: the agent speaks beaver natively — it
holds a ``db`` handle and calls ``db.dict``/``db.queue`` directly.

Execution meets lifecycle at **deposit time** (§12.5): before advancing into
another class the agent reads that class's gate row (``db.dict("gates")``,
written only by the Runtime) — a disabled class blocks every non-error deposit,
surfaced as a visible ``error`` result on the level's ``end_of_level_queue``,
while an **error-result deposit is exempt** and always proceeds (§12.5.5).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from beaver import AsyncBeaverDB
from pydantic import BaseModel, ValidationError

from legio.flow import (
    CONTROL_MESSAGE_TYPE,
    STATE_REPORT_SCOPE,
    AgentStateReport,
    ControlAction,
    ControlMessage,
    ControlVerifier,
    ExecutionRequestMessage,
    ExecutionResultMessage,
    ReportedState,
)
from legio.naming import ActivityState, queue_key
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
        control_verifier: ControlVerifier | None = None,
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
        # The class gate (``db.dict("gates")``, §12.5) is written only by the
        # Runtime and READ by any depositor — a submit or an internal task.
        # This handle is read-only; the agent never writes a gate row.
        self._gates = db.dict("gates")
        # The authenticated control channel (LEG-082): the verify-only handle
        # injected at materialization (None → the loop still consumes and
        # visibly drops control items — never silent, rule 9), the in-memory
        # pause latch and the per-instance anti-replay sequence.
        self._control_verifier = control_verifier
        self._control_paused = False
        self._last_control_seq = 0
        self._control_instance: str | None = None
        # While the loop is parked (a ``disable`` honored) inbox work is held
        # in-memory — never requeued/re-popped (that would churn the queue),
        # and released losslessly on ``enable``/``terminate_with_drain``.
        # Accepted crash semantics (LEG-103 Slice 5d): a crash while parked
        # loses held items — lossless only on the honored control paths above,
        # never via requeue churn.
        self._paused_hold: list[dict[str, Any]] = []

    def set_hooks(self, *, monitor: Monitor | None = None) -> None:
        """Register the optional ``monitor`` observability hook."""
        self._monitor = monitor

    @property
    def agent_id(self) -> str:
        """The agent's stable identity (its queue/namespace name)."""
        return self._agent_id

    async def run(self) -> int:
        """Poll the queue until idle; return the number of steps processed.

        Death-march steps are structurally impossible (LEG-070 rejects
        cycles at load; a route is forward-only, level + 1 per hop), so the
        loop's only termination is the empty queue — no arbitrary cap hides
        pending work (rule 9). Idle returns 0 without sleeping (rule 8).
        """
        steps = 0
        while await self.process_next():
            steps += 1
        return steps

    async def process_next(self) -> bool:
        """Consume at most one work item from the class inbox; else return False.

        The native beaver queue ``get(block=False)`` pops destructively: there
        is no lease, no ``next_run_at`` gate and no re-queue — the item is
        consumed once and routed. Idle returns ``False`` (rule 8, polling only).
        A composite overrides this poll with its two-inlet intake + gated
        collection cycle (§12.3).
        """
        try:
            qitem = await self._queue.get(block=False)
        except IndexError:
            logger.debug("agent idle agent=%s", self._agent_id)
            return False
        await self._process_inbox_item(dict(qitem.data))
        return True

    async def standing_loop(self, instance_id: str) -> None:
        """The instance's long-lived interior loop (LEG-082): suspend on the
        class queue and honor authenticated control between dispatches.

        This is the loop the real bring-up (LEG-087) spawns: the agent lives
        while it runs, and its return **is** the agent's cooperative exit.
        It suspends on the class queue (beaver ``get(block=True)`` — the
        sanctioned "the agent suspends in its own queue"; beaver's internal
        producer-interleave yield is the queue's consumption mechanism, not an
        engine timer — rule 8). Between dispatches a validated control message
        is honored: ``disable`` parks the loop locally (inbox work is held
        in-memory, lossless; the loop stays alive so ``enable`` can arrive),
        ``enable`` releases the held work and resumes, and
        ``terminate_with_drain`` releases any held work, finishes whatever is
        in flight and returns — the agent's cooperative exit.
        """
        self._control_instance = instance_id
        logger.info("agent standing up instance=%s class=%s", instance_id, self._agent_id)
        try:
            while await self._standing_tick():
                pass
        finally:
            logger.info(
                "agent standing down instance=%s class=%s", instance_id, self._agent_id
            )

    async def _standing_tick(self) -> bool:
        """One interior cycle of the standing loop (blocking on the class inbox).

        Atomic agents inherit this: wait for the next inbox item and dispatch
        it (control or work). Returns ``False`` when ``terminate_with_drain``
        was honored — the loop, and with it the agent, ends; ``True`` keeps the
        loop living.
        """
        qitem = await self._queue.get(block=True)
        return await self._dispatch_standing_item(dict(qitem.data))

    async def _dispatch_standing_item(self, item: dict[str, Any]) -> bool:
        """Dispatch one class-inbox item inside the standing loop (LEG-082).

        A control message (``message_type == "control"``) takes the control
        path; anything else is work (an ``ExecutionRequestMessage`` — partition
        by queue §12.2/§12.3 still holds for results). While the loop is parked
        (a ``disable`` honored) inbox work is held in-memory — never consumed,
        never churned, released losslessly on ``enable``/``terminate`` — and
        control keeps being consumed so ``enable`` can reach the agent.
        """
        if item.get("message_type") == CONTROL_MESSAGE_TYPE:
            return await self._honor_control(item)
        if self._control_paused:
            self._paused_hold.append(dict(item))
            logger.debug(
                "agent paused hold instance=%s class=%s held=%d",
                self._control_instance,
                self._agent_id,
                len(self._paused_hold),
            )
            return True
        await self._process_inbox_item(item)
        return True

    async def _release_hold(self) -> None:
        """Put every held inbox item back on the class queue (lossless)."""
        hold, self._paused_hold = self._paused_hold, []
        for item in hold:
            await self._queue.put(dict(item), priority=0.0)
        if hold:
            logger.info(
                "agent hold released instance=%s class=%s count=%d",
                self._control_instance,
                self._agent_id,
                len(hold),
            )

    async def _deposit_state_report(
        self, message: ControlMessage, state: ReportedState
    ) -> None:
        """Deposit the instance's statement that one control was honored (LEG-095).

        One report per honored control, on the Runtime's node-internal intake
        (``STATE_REPORT_SCOPE``) at neutral priority. The report is unsigned by
        design; the Runtime authenticates it by correlating ``(instance_id,
        action, seq)`` with its own pending-mint ledger — never by trust. A
        failed deposit is a visible crash event (rule 9/11) but **does not
        change the honor decision**: the loop already transitioned.
        """
        try:
            await self._db.queue(STATE_REPORT_SCOPE).put(
                AgentStateReport(
                    instance_id=message.target_instance,
                    action=message.action,
                    seq=message.seq,
                    state=state,
                ).model_dump(mode="json"),
                priority=0.0,
            )
            logger.info(
                "agent state_report instance=%s class=%s action=%s state=%s seq=%s",
                self._control_instance,
                self._agent_id,
                message.action.value,
                state.value,
                message.seq,
            )
        except Exception:
            logger.exception(
                "agent state_report deposit FAILED instance=%s class=%s action=%s state=%s seq=%s",
                self._control_instance,
                self._agent_id,
                message.action.value,
                state.value,
                message.seq,
            )

    async def _honor_control(self, item: dict[str, Any]) -> bool:
        """Validate and honor one control message between dispatches (LEG-082).

        The checks are ordered and every rejection is a visible ``WARNING``,
        never silent (rule 9): a missing verifier, an invalid schema, a failed
        signature, a foreign target (requeued at the back — another instance of
        the pool owns it) and a replay (``seq <= last-seen``, per-instance
        monotonic—a rejected message never advances the counter). A valid
        message updates the anti-replay sequence, drives the loop's pause latch
        or ends the loop (``terminate_with_drain``).
        """
        verifier = self._control_verifier
        if verifier is None:
            logger.warning(
                "control dropped no_verifier instance=%s class=%s",
                self._control_instance,
                self._agent_id,
            )
            return True
        try:
            message = ControlMessage.model_validate(item)
        except ValidationError as exc:
            logger.warning(
                "control dropped invalid_schema instance=%s class=%s problems=%s",
                self._control_instance,
                self._agent_id,
                self._summarize_errors(exc),
            )
            return True
        if not verifier.verify(message):
            logger.warning(
                "control dropped bad_signature instance=%s class=%s seq=%s",
                self._control_instance,
                self._agent_id,
                message.seq,
            )
            return True
        if message.target_instance != self._control_instance:
            await self._queue.put(dict(item), priority=0.0)
            logger.debug(
                "control requeued foreign instance=%s target=%s class=%s",
                self._control_instance,
                message.target_instance,
                self._agent_id,
            )
            return True
        if message.seq <= self._last_control_seq:
            logger.warning(
                "control dropped replay instance=%s class=%s seq=%s last=%s",
                self._control_instance,
                self._agent_id,
                message.seq,
                self._last_control_seq,
            )
            return True
        self._last_control_seq = message.seq
        action = message.action
        if action is ControlAction.DISABLE:
            self._control_paused = True
            logger.info(
                "agent disable instance=%s class=%s origin=%s",
                self._control_instance,
                self._agent_id,
                message.origin.value,
            )
            await self._deposit_state_report(message, ReportedState.PARKED)
        elif action is ControlAction.ENABLE:
            self._control_paused = False
            await self._release_hold()
            logger.info(
                "agent enable instance=%s class=%s origin=%s",
                self._control_instance,
                self._agent_id,
                message.origin.value,
            )
            await self._deposit_state_report(message, ReportedState.READY)
        elif action is ControlAction.TERMINATE_WITH_DRAIN:
            await self._release_hold()
            logger.info(
                "agent terminate_with_drain instance=%s class=%s origin=%s",
                self._control_instance,
                self._agent_id,
                message.origin.value,
            )
            await self._deposit_state_report(message, ReportedState.TERMINATING)
            return False
        return True

    async def _process_inbox_item(self, item: dict[str, Any]) -> None:
        """Consume one item from the class inbox (an entry request) and run it.

        Every message that ever lands on an inbox is an
        ``ExecutionRequestMessage``: results are partitioned onto the closer's
        queue (final-result or a composite's gathering) by construction
        (§12.2/§12.3), so no message-type dispatch happens here.
        """
        try:
            request = ExecutionRequestMessage.model_validate(item)
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
        No contract declared → no check. It applies to **entry requests only** —
        by construction a fan-in result never lands on an inbox (partition by
        queue, §12.2/§12.3), so there is no result-kind exemption to
        special-case here.
        """
        model = self._input_contract
        if model is None:
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

    async def _class_gate_open(self, class_name: str) -> bool:
        """Whether a deposit may enter a class's queue (§12.5).

        A gate row ``{state: disabled}`` closes entry for every depositor —
        submit and internal deposits alike (§12.5.1/§12.5.2). A missing row is
        open. Only the Runtime writes gate rows; this is a plain read.
        """
        gate = await self._gates.fetch(class_name)
        if gate is None:
            return True
        return gate.get("state") != ActivityState.DISABLED.value

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

        The §12.5 deposit-time gate handshake lives on the advance: a non-error
        deposit into a disabled class is **blocked** — nothing enters a
        non-enabled class (§12.5.1) — and surfaces as a visible ``error`` result
        on this level's ``end_of_level_queue`` (§12.5.5), never a silent drop.
        An **error-result deposit is exempt** and always proceeds (§12.5.5): a
        closed class keeps draining and an error must reach its return path.
        """
        next_index = request.current_index + 1
        if next_index < len(request.level_route):
            next_step = request.level_route[next_index]
            next_class, next_input_as = next_step
            if not await self._class_gate_open(next_class) and "error" not in payload:
                logger.warning(
                    "agent deposit blocked agent=%s task=%s to=%s gate=closed",
                    self._agent_id,
                    request.task_id,
                    next_class,
                )
                await self._deposit_result(
                    request,
                    {
                        "error": (
                            f"deposit blocked: class {next_class!r} is disabled "
                            f"(gate closed at advance, task={request.task_id})"
                        )
                    },
                )
                return
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
        await self._deposit_result(request, payload)

    async def _deposit_result(
        self, request: ExecutionRequestMessage, payload: dict[str, Any]
    ) -> None:
        """Close the level: deposit ``payload`` as an ``ExecutionResultMessage``
        to ``request.end_of_level_queue`` (the result/gather queue — never a
        class, so no gate handshake applies)."""
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
