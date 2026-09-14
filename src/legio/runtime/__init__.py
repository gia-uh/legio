"""legio.runtime — the Runtime public face of the runtime triangle (§0/§5/§6/§12.5).

The ``Runtime`` is the orchestrator and public face: it has the initiative. It
receives the business ``submit``/``status``, owns the class lifecycle verbs
(create / recreate / enable / disable / destroy at both the class and the
instance level), and orchestrates the **Manager** (the task engine) and the
**Registry** (the catalog / instances / yaml cache). Every mutation follows the
pattern **action → (Manager fact) → confirm (Manager read) → record (Registry,
posteriori)** — the Runtime is the translator between the Manager's task
language and the Registry's catalog language, and it never executes agent work
itself.

Information ownership (§6): the **Manager holds the task reality** — the
business submit rides ``Manager.submit_task`` as a ``seed`` task whose callable
deposits the root ``ExecutionRequestMessage`` (ARCHITECTURE §7), the bring-up
rides the ``bring_up`` fact (one-shot default; a boot mounting real agents
overrides it with the **parked async generator** that hosts each instance's
standing loop, LEG-087), and the instance lifecycle verbs (enable / disable /
destroy) ride the ``enable_instance`` / ``disable_instance`` / ``destroy_instance``
facts, each of which mints a signed ``ControlMessage`` (LEG-082) at control
priority on the target class queue. Operator lifecycle orders (CLI/API/peer,
LEG-081) enter through the Runtime's own ``node_ops`` intake and are drained by
the ``NODE_OP`` polling fact, which relays each validated intent to its
lifecycle verb (LEG-088) — the Runtime keeps deciding, the Manager keeps
executing, the Registry keeps mirroring. The **Registry is the posterior
mirror**, written only by the Runtime after each fact is confirmed. The
**Runtime is the decision point** between them. It reaches the Manager **only
through its public API** (``submit_task``/``status``) and the Registry through
its public surface — it never opens another layer's beaver scopes.

Beaver footprint (rule 13): the Runtime owns exactly **two** direct scopes —
the class entry gate (``db.dict("gates")``, §12.5) and the operator control
intake (``db.queue("node_ops")``, LEG-088 — the ``origin: operator`` source,
drained **via the ``NODE_OP`` Manager fact**, never pumped). The Manager owns
``tasks``/``pending_tasks``/``control`` (business seeds land there as Manager
tasks); the Registry owns ``catalog``/``instances``/``yaml_cache``. Class/result
queues (``legio:queue:<...>``) are the flow's shared message medium, deposited
by the seed and polled by agents — not a layer's private scope. In-process,
never persisted: the instance → bring-up-task map (no 1:1 instance ↔ task
identity mapping, §4.8).

The **node owns the executor**: the Manager is driven by the node's polling loop
(``manager.run()``), never pumped by the Runtime. The Runtime's confirms are
bounded clock waits over the ``lifecycle`` config budgets (§5.8/§10.2, rule 8
exception) — the same ``drain_timeout``/``drain_interval`` the queue drain uses.
Each **real bring-up occupies one executor dispatch for the agent's life**
(a parked generator awaiting its standing loop; §6.1's multi-executor is
sanctioned): the record reads ``running`` while the agent lives and the Manager
writes ``success`` at the exact structural moment the agent's loop ends — never
a timer or a poll. A boot mounting real agents overwrites the one-shot bring-up
with the real one (documented seam, step 4).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import suppress
from enum import Enum
from typing import Any, Literal, cast

from beaver import AsyncBeaverDB
from pydantic import BaseModel, ValidationError

from legio.agents import AgentBase
from legio.config import LifecycleConfig, PoolsConfig
from legio.errors import RecoverableError
from legio.flow import (
    CONTROL_PRIORITY,
    STATE_REPORT_SCOPE,
    AgentStateReport,
    ControlAction,
    ExecutionRequestMessage,
    ExecutionResultMessage,
    FlowToken,
    ReportedState,
    derive_control_key,
    sign_control,
)
from legio.manager import Manager, TaskRecord, TaskStatus
from legio.naming import queue_key, result_queue_key, validate_node_id, validate_task_id
from legio.patterns import Catalog, load_patterns
from legio.patterns.schema1 import AgentKind, AgentSpec, AgentType
from legio.registry import ActivityState, ClassRecord, InstanceRecord, Registry

logger = logging.getLogger(__name__)

BRING_UP_TASK = "bring_up"
SEED_TASK = "seed"
ENABLE_INSTANCE = "enable_instance"
DISABLE_INSTANCE = "disable_instance"
DESTROY_INSTANCE = "destroy_instance"

# LEG-088 — the node control intake: a Runtime-owned queue of typed operator
# intents, drained via the ``NODE_OP`` Manager fact (naming deliberately
# distinct from the Manager's ``control`` task-control scope — the Manager
# controls *tasks*, ``node_ops`` carries *operator intents*).
NODE_OP_TASK = "node_op"
NODE_OPS_SCOPE = "node_ops"
# LEG-095 — the state-report intake: the agent's honor statements land on the
# node-internal ``state_report`` queue (plain beaver naming, the ``node_ops``
# pattern) and are drained via this Manager fact (the intake's scope constant
# ``STATE_REPORT_SCOPE`` itself lives in ``legio.flow.control`` — the agent
# deposits there and cannot import the Runtime).
STATE_REPORT_TASK = "state_report"
CLASS_OP_VERBS = frozenset({"enable_class", "disable_class", "destroy_class"})
INSTANCE_OP_VERBS = frozenset({"enable_instance", "disable_instance", "destroy_instance"})
NODE_OP_VERBS = CLASS_OP_VERBS | INSTANCE_OP_VERBS

_NODE_OP_VERB = Literal[
    "enable_class",
    "disable_class",
    "destroy_class",
    "enable_instance",
    "disable_instance",
    "destroy_instance",
]


class NodeOp(BaseModel):
    """A typed operator intent on the node's intake (LEG-088 §A).

    Small, domain-free payload: the lifecycle verb and the class/instance it
    addresses. The Runtime decides which intents a caller may deposit through
    the intake; the ``NODE_OP`` drain relays a validated intent to the
    corresponding lifecycle verb, which mints and deposits the signed control
    message (LEG-087).
    """

    verb: _NODE_OP_VERB
    class_name: str
    instance_id: str | None = None


class TaskState(str, Enum):
    """Lifecycle state of a business task (the semantics of §7.7)."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"


class TaskEntry(BaseModel):
    """A read-only view of a business task returned by the Runtime's ``status``."""

    task_id: str
    owner: str
    token: FlowToken
    state: TaskState
    output: dict[str, Any] | None = None
    result_key: str | None = None


class WorkItemReceipt(BaseModel):
    """The acceptor's answer to a deposited work item (LEG-092).

    ``id`` is the author's own task id. ``deposited`` is true when the acceptor
    minted a genuine business task for it; ``deduplicated`` is true when the
    same id is already known — the work is never executed twice (LEG-093
    prerequisite). Both fields may not be true together; a deduplicated receipt
    is a decision, never an error.
    """

    id: str
    deposited: bool
    deduplicated: bool = False


class Runtime:
    """The orchestrator and public face of the runtime triangle (§0/§6).

    ``Runtime`` owns the class entry gate (§12.5), the operator control intake
    (``node_ops``, LEG-088 — drained via the ``NODE_OP`` Manager fact), the
    business ``submit``/``status`` (mounted on ``Manager.submit_task``, §7.1)
    and the class/instance lifecycle verbs. It orchestrates the injected (or
    default) ``Manager`` and ``Registry`` and never runs agent work itself.
    """

    def __init__(
        self,
        db: AsyncBeaverDB,
        *,
        node_id: str,
        manager: Manager | None = None,
        registry: Registry | None = None,
        lifecycle: LifecycleConfig | None = None,
        control_key: bytes | None = None,
    ) -> None:
        """Bind the runtime to the connected beaver substrate and its node id.

        ``node_id`` must be a valid ``<node>@<host>`` identity (never the
        reserved ``local``). An explicit ``manager``/``registry`` may be
        injected; otherwise defaults are built on the same substrate.
        ``lifecycle`` provides the bounded clock-wait budgets (drain and the
        lifecycle confirms, §5.8/§10.2); absent → built-in defaults.
        ``control_key`` is the per-boot, in-process key the lifecycle facts
        mint with (LEG-082/§2.2); absent → derived from an entropy draw
        (random per boot, never persisted, never logged — rule 11).
        """
        if db is None:
            raise TypeError(
                "Runtime requires a connected AsyncBeaverDB (beaver system substrate)"
            )
        validate_node_id(node_id)
        self._node_id = node_id
        self._db = db
        self.manager = manager if manager is not None else Manager(db, node_id=node_id)
        self.registry = registry if registry is not None else Registry(db)
        self._lifecycle = lifecycle if lifecycle is not None else LifecycleConfig()
        self._gates = db.dict("gates")
        # LEG-088: the node control intake ('node_ops') — the Runtime's second
        # scope. Operator intents land here; the ``NODE_OP`` Manager fact drains
        # it (never the Runtime pumping). The Manager owns ``control`` and
        # ``pending_tasks``; this ownership split never collides (rule 13).
        self._node_ops = db.queue(NODE_OPS_SCOPE)
        # LEG-095: the agent honor-statement intake, drained via the
        # ``STATE_REPORT`` Manager fact, and the Runtime's own pending-mint
        # ledger (mint time → matching report applied); the intake never trusts
        # a report, it correlates it with the ledger (LEG-095 §A/§C).
        self._state_reports = db.queue(STATE_REPORT_SCOPE)
        self._pending_controls: dict[tuple[str, str, int], str] = {}
        self._instance_tasks: dict[tuple[str, str], str] = {}
        self._instance_sequence: dict[str, int] = {}
        # The authenticated control channel (LEG-082): the Runtime is the only
        # minting authority (origin=operator); the key is in-process, per-boot.
        self._control_key = control_key if control_key is not None else derive_control_key(
            os.urandom(32)
        )
        self._control_sequence: dict[tuple[str, str], int] = {}
        # The standing agent map, mounted by the boot (LEG-087): the real
        # bring-up binds ``standing_loop`` on an agent of the target class.
        self._agents: dict[str, AgentBase] = {}
        self._live_loops: dict[str, set[str]] = {}
        self.manager.register(BRING_UP_TASK, self._bring_up_impl)
        self.manager.register(SEED_TASK, self._seed_impl)
        for fact in (ENABLE_INSTANCE, DISABLE_INSTANCE, DESTROY_INSTANCE):
            self.manager.register(fact, self._instance_control_fact)
        self.manager.register(NODE_OP_TASK, self._node_op_fact)  # LEG-088 intake drain
        self.manager.register(STATE_REPORT_TASK, self._state_report_fact)  # LEG-095 drain
        logger.info("runtime up node=%s", node_id)

    # --- registered callables (the facts the Manager executes) ----------------

    async def _bring_up_impl(self, class_name: str, instance_id: str, queue: str) -> str:
        """One-shot bring-up fact: the agent is up (§5.1 step 2).

        A boot mounting real agents **overwrites this callable** with the real
        bring-up (documented seam; the interior of the agent's own loop is
        step 4). The Runtime confirms the terminal fact and records the instance
        posteriori — it never runs the agent itself.
        """
        return instance_id

    def mount_agents(self, agents: Mapping[str, AgentBase]) -> None:
        """Mount the materialized standing agents and the real bring-up (boot).

        The boot (LEG-087) derives the per-boot control key, computes the
        verify-only handles, materializes the agent map with them injected
        (LEG-082 param) and hands it here: the Runtime then registers the **real
        bring-up** — a parked async generator that spawns the instance's own
        ``standing_loop`` — overriding the one-shot default.
        """
        self._agents = dict(agents)
        self.manager.register(BRING_UP_TASK, self._real_bring_up)
        logger.info("runtime agents mounted count=%d", len(self._agents))

    async def _real_bring_up(
        self, class_name: str, instance_id: str, queue: str
    ) -> AsyncGenerator[str]:
        """Real bring-up: a parked async generator hosting the agent's life.

        Spawns ``standing_loop(instance_id)`` (LEG-082), yields the instance id
        once and then **awaits the loop**: the Manager parks the generator and
        the record reads ``running`` for the agent's whole life. The agent's
        cooperative exit — a ``terminate_with_drain`` honored on its own queue —
        finishes the loop; ``await`` returns, the generator falls out and the
        Manager writes ``success`` at that same structural moment (no timers, no
        polls, rule 8). Closing the generator early (a manager-level cancel while
        parked at the yield) runs the ``finally``: the standing loop is cancelled
        before a ``failed(cancelled)`` is written — the listener never leaks.
        """
        try:
            agent = self._agents[class_name]
        except KeyError as exc:
            logger.error("runtime bring_up no_agent class=%s", class_name)
            raise RecoverableError(
                f"no standing agent mounted for class {class_name!r}; re-boot binds it"
            ) from exc
        previous = self._live_loops.get(class_name)
        if previous:
            logger.warning(
                "runtime real bring_up shared_agent second_loop class=%s "
                "instances=%s (pool>1 control state is documented debt)",
                class_name,
                ",".join(sorted(previous)),
            )
        self._live_loops.setdefault(class_name, set()).add(instance_id)
        loop_task = asyncio.create_task(agent.standing_loop(instance_id))
        logger.info("runtime real bring_up class=%s instance=%s", class_name, instance_id)
        try:
            yield instance_id
            await loop_task
        finally:
            if not loop_task.done():
                loop_task.cancel()
                with suppress(asyncio.CancelledError):
                    await loop_task
            loops = self._live_loops.get(class_name)
            if loops is not None:
                loops.discard(instance_id)
                if not loops:
                    self._live_loops.pop(class_name, None)

    # --- instance lifecycle facts (the Runtime mints, the Manager executes) ---

    async def _instance_control_fact(
        self, class_name: str, instance_id: str, action: str
    ) -> dict[str, Any]:
        """The enable/disable/destroy fact: mint a signed control message (LEG-082)
        with the per-boot key and deposit it at control priority on the target
        instance's class queue. The Runtime (operator origin) is the only
        minting authority; the message is honored between the agent's dispatches.

        The mint also **enters the pending report ledger** (LEG-095): the entry
        ``(instance_id, action, seq)`` lives from mint until the matching honor
        report is consumed by ``_state_report_fact`` — the intake correlates a
        report with exactly this ledger, never by trust.
        """
        seq = self._control_sequence.get((class_name, instance_id), 0) + 1
        self._control_sequence[(class_name, instance_id)] = seq
        self._pending_controls[(instance_id, action, seq)] = class_name
        logger.info(
            "runtime control minted class=%s instance=%s action=%s seq=%s "
            "pending_report=True",
            class_name,
            instance_id,
            action,
            seq,
        )
        message = sign_control(
            self._control_key,
            target_instance=instance_id,
            action=ControlAction(action),
            seq=seq,
        )
        await self._db.queue(queue_key(class_name)).put(
            message.model_dump(mode="json"), priority=CONTROL_PRIORITY
        )
        await self.manager.submit_task(STATE_REPORT_TASK)  # kick the intake drain
        return message.model_dump(mode="json")

    async def _seed_impl(
        self, client_id: str, token: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any]:
        """The business-seed fact (§7.1): validate the root token and deposit the
        root ``ExecutionRequestMessage`` into the starting agent's queue. Returns
        the validated token (the Manager records it as the seed's result).
        """
        flow_token = FlowToken.model_validate(token)
        if not flow_token.root:
            logger.error(
                "runtime seed deny task=%s root=%s", flow_token.task_id, flow_token.root
            )
            raise RecoverableError(f"seed token is not a root token (task {flow_token.task_id!r})")
        request = ExecutionRequestMessage(
            level_route=flow_token.level_route,
            current_index=0,
            end_of_level_queue=flow_token.end_of_level_queue,
            level=1,
            launcher_class=flow_token.launcher_class,
            task_id=flow_token.task_id,
            branch_id=flow_token.branch_id,
            payload=payload,
        )
        await self._db.queue(queue_key(flow_token.launcher_class)).put(
            request.model_dump(mode="json"), priority=0.0
        )
        logger.info(
            "runtime seed task=%s owner=%s class=%s result_queue=%s",
            flow_token.task_id,
            client_id,
            flow_token.launcher_class,
            flow_token.end_of_level_queue,
        )
        return flow_token.model_dump(mode="json")

    # --- node control intake (LEG-088) -------------------------------------------

    async def deposit_node_op(
        self, verb: str, class_name: str, instance_id: str | None = None
    ) -> str:
        """Deposit an operator lifecycle intent on the ``node_ops`` intake.

        The Runtime decides which intents a caller may deposit (§A): the verb
        must be a known lifecycle verb, the class must live in the catalog, and
        an instance verb must address a live instance. On the way in, the intake
        **only** queues the typed payload and schedules a ``NODE_OP`` drain —
        nothing is minted and no agent queue is touched (the drain relays the
        validated intent to the lifecycle verb, which mints/deposits/records).
        Returns the drain task id, so the operator can read the intent's
        outcome. Rejections are visible (rule 9) and never mint anything.
        """
        op = self._validate_node_op(verb, class_name, instance_id)
        if await self.registry.class_state(op.class_name) is None:
            logger.warning(
                "runtime node_op deny verb=%s class=%s (unknown class)",
                op.verb,
                op.class_name,
            )
            raise RecoverableError(f"unknown class {op.class_name!r}")
        if op.verb in INSTANCE_OP_VERBS and op.instance_id is not None:
            instance = await self.registry.get_instance(op.class_name, op.instance_id)
            if instance is None:
                logger.warning(
                    "runtime node_op deny verb=%s class=%s instance=%s (unknown instance)",
                    op.verb,
                    op.class_name,
                    op.instance_id,
                )
                raise RecoverableError(
                    f"unknown instance {op.instance_id!r} of class {op.class_name!r}"
                )
        await self._node_ops.put(op.model_dump(mode="json"), priority=0.0)
        task_id = await self.manager.submit_task(NODE_OP_TASK)
        logger.info(
            "runtime node_op deposit verb=%s class=%s instance=%s task=%s",
            op.verb,
            op.class_name,
            op.instance_id or "-",
            task_id,
        )
        return task_id

    async def _node_op_fact(self) -> dict[str, Any]:
        """The intake drain fact (LEG-088 §B): pop one intent off ``node_ops``,
        relay it through the Runtime's decision logic to the lifecycle verb,
        and schedule one more drain while work remains. It is a drain loop whose
        scheduling **field** is the presence of work on the intake — a data
        read, never a sleep (rule 8).
        """
        try:
            item = await self._node_ops.get(block=False)
        except IndexError:
            return {"processed": 0, "replenished": False}
        try:
            op = NodeOp.model_validate(item.data)
        except ValidationError as exc:
            raise RecoverableError(
                f"invalid node op on intake {item.data!r}: {exc}"
            ) from exc
        await self._apply_node_op(op)
        replenished = await self._node_ops.count() > 0
        if replenished:
            await self.manager.submit_task(NODE_OP_TASK)
        logger.info(
            "runtime node_op intake applied verb=%s class=%s instance=%s replenished=%s",
            op.verb,
            op.class_name,
            op.instance_id or "-",
            replenished,
        )
        return {"processed": 1, "replenished": replenished}

    async def _state_report_fact(self) -> dict[str, Any]:
        """The state-report intake drain (LEG-095 §C): mirrors ``_node_op_fact``.

        Pop one agent honor report off ``state_report``, validate it, apply it
        to the Registry state and re-schedule one drain while reports remain
        (scheduling field = presence on the intake, a data read, never a sleep,
        rule 8). Validation is by **self-correlation**: the pending-mint ledger
        must hold exactly ``(instance_id, action, seq)`` and the instance must
        still exist — the intake single-writes the Registry state only on a full
        hit (no optimism). Each miss is a visible ``WARNING``, the item is
        consumed and never applied (rule 9); the pending entry of a *different*
        control is left untouched.
        """
        try:
            item = await self._state_reports.get(block=False)
        except IndexError:
            return {"processed": 0, "replenished": False}
        report = AgentStateReport.model_validate(item.data)
        class_name = self._pending_controls.pop(
            (report.instance_id, report.action.value, report.seq), None
        )
        replenished = await self._state_reports.count() > 0
        if class_name is None:
            logger.warning(
                "runtime state_report orphan instance=%s action=%s seq=%s "
                "(no pending mint; consumed, not applied)",
                report.instance_id,
                report.action.value,
                report.seq,
            )
            if replenished:
                await self.manager.submit_task(STATE_REPORT_TASK)
            return {"processed": 1, "replenished": replenished}
        if await self.registry.get_instance(class_name, report.instance_id) is None:
            logger.warning(
                "runtime state_report unknown_instance instance=%s class=%s action=%s "
                "seq=%s (consumed, not applied)",
                report.instance_id,
                class_name,
                report.action.value,
                report.seq,
            )
            if replenished:
                await self.manager.submit_task(STATE_REPORT_TASK)
            return {"processed": 1, "replenished": replenished}
        target = {
            ReportedState.READY: ActivityState.ENABLED,
            ReportedState.PARKED: ActivityState.DISABLED,
        }.get(report.state)
        if target is not None:
            await self.registry.set_instance_state(
                class_name, report.instance_id, target
            )
            logger.info(
                "runtime state_report applied instance=%s class=%s action=%s "
                "state=%s seq=%s",
                report.instance_id,
                class_name,
                report.action.value,
                target.value,
                report.seq,
            )
        else:
            logger.info(
                "runtime state_report terminating (informational) instance=%s "
                "class=%s seq=%s",
                report.instance_id,
                class_name,
                report.seq,
            )
        if replenished:
            await self.manager.submit_task(STATE_REPORT_TASK)
        return {"processed": 1, "replenished": replenished}

    async def _apply_node_op(self, op: NodeOp) -> str:
        """The Runtime's decision logic for a drained intent: relay it to the
        corresponding lifecycle verb — which mints the signed control message,
        confirms, and records the Registry posteriori (LEG-087). The Runtime
        keeps deciding; the Manager keeps executing; the Registry keeps
        mirroring. An unknown verb fails the drain visibly (rule 9) and never
        mints anything.
        """
        if op.verb == "enable_class":
            await self.enable_class(op.class_name)
        elif op.verb == "disable_class":
            await self.disable_class(op.class_name)
        elif op.verb == "destroy_class":
            await self.destroy_class(op.class_name)
        elif op.verb == "enable_instance":
            await self.enable_instance(op.class_name, self._require_op_instance(op))
        elif op.verb == "disable_instance":
            await self.disable_instance(op.class_name, self._require_op_instance(op))
        elif op.verb == "destroy_instance":
            await self.destroy_instance(op.class_name, self._require_op_instance(op))
        else:
            raise RecoverableError(f"unknown node op verb {op.verb!r}")
        logger.info(
            "runtime node_op applied verb=%s class=%s instance=%s",
            op.verb,
            op.class_name,
            op.instance_id or "-",
        )
        return op.verb

    @staticmethod
    def _require_op_instance(op: NodeOp) -> str:
        """An instance verb must address an instance (defensive rule 9: the
        drain re-checks an intent's shape even after a deposit-side check)."""
        if op.instance_id is None:
            raise RecoverableError(
                f"node op verb {op.verb!r} requires an instance (intent shape broken)"
            )
        return op.instance_id

    def _validate_node_op(self, verb: str, class_name: str, instance_id: str | None) -> NodeOp:
        """Build and shape-check an intent (§A): an unknown verb is refused
        visibly at the intake; instance verbs require an instance and class
        verbs forbid one."""
        try:
            op = NodeOp(verb=cast(_NODE_OP_VERB, verb), class_name=class_name, instance_id=instance_id)
        except ValidationError as exc:
            raise RecoverableError(f"unknown node op verb {verb!r}") from exc
        if op.verb in INSTANCE_OP_VERBS and op.instance_id is None:
            raise RecoverableError(
                f"node op verb {op.verb!r} requires an instance"
            )
        if op.verb in CLASS_OP_VERBS and op.instance_id is not None:
            raise RecoverableError(
                f"node op verb {op.verb!r} is a class verb and does not take an instance"
            )
        return op

    # --- bounded clock waits over the lifecycle budgets (§5.8/§10.2) ---------------

    async def _lifecycle_budget(self, class_name: str) -> tuple[float, float]:
        """Resolve the human-scale ``(drain_timeout, drain_interval)`` for a class."""
        kind = await self.registry.class_kind(class_name)
        params = self._lifecycle.resolve(class_name, per_kind=kind)
        return cast(float, params.drain_timeout), cast(float, params.drain_interval)

    async def _await_observable_state(
        self, task_id: str, ok: Callable[[TaskRecord], bool], *, what: str, class_name: str
    ) -> TaskRecord:
        """Read-poll the Manager record until ``ok`` holds (bounded clock wait).

        A FAILED record that ``ok`` does not accept raises immediately with its
        error (rule 9). Exhausting the ``drain_timeout`` budget raises a visible
        ``RecoverableError`` carrying the last observed state.
        """
        timeout, interval = await self._lifecycle_budget(class_name)
        deadline = time.monotonic() + timeout
        last: TaskRecord | None = None
        while time.monotonic() < deadline:
            record = await self.manager.status(task_id)
            last = record
            if record is not None:
                if ok(record):
                    return record
                if record.status == TaskStatus.FAILED:
                    error = record.error or "unknown error"
                    raise RecoverableError(f"{what}: task {task_id!r} failed: {error}")
            await asyncio.sleep(interval)
        state = last.status.value if last is not None else "missing"
        raise RecoverableError(
            f"{what}: could not confirm task {task_id!r} within {timeout}s (last state: {state})"
        )

    async def _await_instance_state(
        self,
        class_name: str,
        instance_id: str,
        target: ActivityState,
        *,
        what: str,
    ) -> None:
        """Confirm an instance reached ``target`` by report convergence (LEG-095).

        Read-poll the Registry state until it equals the target, bounded by the
        class lifecycle budget (the sanctioned §5.8 bounded clock wait, exactly
        the ``_await_observable_state`` pattern). While waiting the loop also
        schedules a ``STATE_REPORT`` intake drain whenever reports sit on the
        ``state_report`` queue — the agent cannot reach the Manager, so the seed
        of the drain comes from here (data-read scheduling, rule 8). A ``FAILED``
        bring-up record raises immediately with its error; exhausting the budget
        raises a visible ``RecoverableError`` ("no state report") — never a
        silent grey state.
        """
        timeout, interval = await self._lifecycle_budget(class_name)
        task_id = self._instance_tasks.get((class_name, instance_id))
        deadline = time.monotonic() + timeout
        last: str | None = None
        while time.monotonic() < deadline:
            if await self._state_reports.count() > 0:
                await self.manager.submit_task(STATE_REPORT_TASK)
            instance = await self.registry.get_instance(class_name, instance_id)
            if instance is None:
                last = "missing"
            else:
                last = instance.state.value
                if instance.state is target:
                    return
            if task_id is not None:
                record = await self.manager.status(task_id)
                if (
                    record is not None
                    and record.status == TaskStatus.FAILED
                    and instance is not None
                ):
                    error = record.error or "unknown error"
                    raise RecoverableError(f"{what}: bring-up task failed: {error}")
            await asyncio.sleep(interval)
        raise RecoverableError(
            f"{what}: no state report within {timeout}s (last state: {last!r})"
        )

    # --- bring-up vehicle -----------------------------------------------------

    async def _next_instance_id(self, class_name: str) -> str:
        """Mint the next instance id for a class: monotonic intraboot (§5.1/§5.2).

        The per-class sequence starts at the highest recorded instance suffix
        (reseeded from the registry on restart) and only ever increases, so an
        id is never reused while an earlier instance of the same class exists.
        """
        seq = self._instance_sequence.get(class_name)
        if seq is None:
            recorded = await self.registry.list_instances(class_name)
            highest = 0
            for instance in recorded:
                suffix = instance.instance_id.rsplit("-", 1)[-1]
                if suffix.isdigit():
                    highest = max(highest, int(suffix))
            seq = highest
        seq += 1
        self._instance_sequence[class_name] = seq
        return f"{class_name}-{seq}"

    async def _bring_up(self, class_name: str, born_state: ActivityState) -> str:
        """Create one instance: mint, submit the bring-up fact, confirm it, and
        record the instance posteriori (§5.1/§5.2)."""
        instance_id = await self._next_instance_id(class_name)
        queue = queue_key(class_name)
        task_id = await self.manager.submit_task(BRING_UP_TASK, class_name, instance_id, queue)
        self._instance_tasks[(class_name, instance_id)] = task_id
        await self._await_observable_state(
            task_id,
            lambda record: record.status in (TaskStatus.RUNNING, TaskStatus.SUCCESS),
            what=f"bring_up class={class_name} instance={instance_id}",
            class_name=class_name,
        )
        await self.registry.record_instance(class_name, instance_id, state=born_state)
        logger.info(
            "runtime bring_up class=%s instance=%s task=%s born=%s",
            class_name,
            instance_id,
            task_id,
            born_state.value,
        )
        return instance_id

    # --- business submit / status (mounted on the Manager, §7.1/§7.7) ---------

    async def submit(
        self, client_id: str, route: tuple[tuple[str, str], ...], payload: dict[str, Any]
    ) -> str:
        """Submit a business task; returns its ``task_id`` (``<node_id>:<uuid>``).

        The class entry gate (§12.5) is checked against ``route[0][0]`` first —
        a disabled class rejects the submission before anything is minted or
        deposited. On the way through, the payload is re-keyed under the
        starting agent's ``input_as`` and the root ``ExecutionRequestMessage``
        is deposited by the ``seed`` task the Manager runs (ARCHITECTURE §7.1):
        the business record is a genuine Manager task in ``tasks`` — the
        decoupled, dynamic submit the docs prescribe.
        """
        if not route:
            raise ValueError("route must contain at least one agent")
        first_class, first_input_as = route[0]
        gate = await self._gates.fetch(first_class)
        if gate is not None and gate.get("state") == ActivityState.DISABLED.value:
            logger.warning("runtime submit denied class=%s by=client", first_class)
            raise RecoverableError(f"class {first_class!r} is disabled (entry gate closed)")
        task_id = f"{self._node_id}:{uuid.uuid4()}"
        validate_task_id(task_id)
        result_queue = result_queue_key(task_id)
        rekeyed_payload = {first_input_as: payload}
        root_branch_id = str(uuid.uuid4())
        token = FlowToken(
            level_route=route,
            current_index=0,
            end_of_level_queue=result_queue,
            level=1,
            launcher_class=first_class,
            task_id=task_id,
            branch_id=root_branch_id,
            root=True,
        )
        await self.manager.submit_task(
            SEED_TASK,
            task_id=task_id,
            client_id=client_id,
            token=token.model_dump(mode="json"),
            payload=rekeyed_payload,
        )
        logger.info(
            "runtime submit task=%s owner=%s class=%s result_queue=%s",
            task_id,
            client_id,
            first_class,
            result_queue,
        )
        return task_id

    async def submit_work_item(
        self,
        author: str,
        route: tuple[tuple[str, str], ...],
        payload: dict[str, Any],
        *,
        task_id: str,
    ) -> WorkItemReceipt:
        """Deposit a federated work item keyed by the *author's* task id (LEG-092).

        Mirrors ``submit`` (same entry gate, same root flow token, same seed
        fact) but the invitee is a remote peer: the task id is minted by the
        author — ``<node_id>:<uuid>`` at the origin — so the result lands where
        the author reads it and so a retried delivery is idempotent. The author
        derived the step against this node's roster (LEG-091); the acceptor
        double-checks nothing but the gate here — interface/scope were resolved
        author-side and re-checked by the HTTP shell around this seam.

        A ``task_id`` the acceptor already knows is **not an error**: the
        Manager rejects the duplicate visibly and this seam translates it into a
        deduplicated receipt — write-before-ack makes a repeated hand-off a
        no-op, never a second execution (LEG-093).
        """
        if not route:
            raise ValueError("route must contain at least one agent")
        first_class, first_input_as = route[0]
        gate = await self._gates.fetch(first_class)
        if gate is not None and gate.get("state") == ActivityState.DISABLED.value:
            logger.warning("runtime work_item denied class=%s by=peer", first_class)
            raise RecoverableError(f"class {first_class!r} is disabled (entry gate closed)")
        validate_task_id(task_id)
        result_queue = result_queue_key(task_id)
        rekeyed_payload = {first_input_as: payload}
        root_branch_id = str(uuid.uuid4())
        token = FlowToken(
            level_route=route,
            current_index=0,
            end_of_level_queue=result_queue,
            level=1,
            launcher_class=first_class,
            task_id=task_id,
            branch_id=root_branch_id,
            root=True,
        )
        try:
            await self.manager.submit_task(
                SEED_TASK,
                task_id=task_id,
                client_id=author,
                token=token.model_dump(mode="json"),
                payload=rekeyed_payload,
            )
        except ValueError as exc:
            logger.info(
                "runtime work_item dedup task=%s class=%s reason=%s",
                task_id,
                first_class,
                exc,
            )
            return WorkItemReceipt(id=task_id, deposited=False, deduplicated=True)
        logger.info(
            "runtime work_item task=%s owner=%s class=%s result_queue=%s deposited=true",
            task_id,
            author,
            first_class,
            result_queue,
        )
        return WorkItemReceipt(id=task_id, deposited=True)

    async def ack_outbox(self, task_id: str) -> bool:
        """Consume the result of a completed work item from its outbox queue.

        The outbox is the task's result queue (``result_queue_key(task_id)``),
        where the agent flow written the ``ExecutionResultMessage`` during
        execution (LEG-093). The ack is a destructive drain: after it, a poll
        reads empty (read-after-ack = empty). An already-empty outbox is a
        no-op (`False`), never an error.
        """
        outbox = self._db.queue(queue_key(result_queue_key(task_id)))
        try:
            await outbox.get(block=False)
        except IndexError:
            return False
        logger.info("runtime ack_outbox task=%s", task_id)
        return True

    async def read_outbox(self, task_id: str) -> dict[str, Any] | None:
        """Peek the result queue for a completed work item (LEG-093).

        Returns the ``ExecutionResultMessage`` payload when the flow has
        already written it, else ``None`` — a non-blocking poll (rule 8). The
        write happened at execution time, independent of any ack; the message
        is left in place for ``ack_outbox`` to consume.
        """
        outbox = self._db.queue(queue_key(result_queue_key(task_id)))
        item = await outbox.peek()
        if item is None:
            return None
        result = ExecutionResultMessage.model_validate(item.data)
        logger.info("runtime read_outbox task=%s ready=true", task_id)
        return dict(result.payload)

    async def status(self, task_id: str, client_id: str | None) -> TaskEntry:
        """Return the business task entry if ``client_id`` owns it, else raise.

        The business record is the Manager's seed task (its ``kwargs`` carry the
        owner); the completed result is read from the task's final-result queue
        via a non-destructive ``peek`` (§7.7). A ``None`` requester (anonymous
        open-mode access) is denied exactly like a foreign client, and a failed
        seed surfaces visibly.
        """
        record = await self.manager.status(task_id)
        if record is None:
            logger.warning("runtime status unknown task=%s", task_id)
            raise KeyError(f"unknown task {task_id!r}")
        if record.kwargs.get("client_id") != client_id:
            logger.warning(
                "runtime status denied task=%s owner=%s requester=%s",
                task_id,
                record.kwargs.get("client_id"),
                client_id,
            )
            raise PermissionError("task is scoped to its owning client")
        if record.status == TaskStatus.FAILED:
            raise RecoverableError(f"task {task_id!r} failed: {record.error or 'unknown error'}")
        token = FlowToken.model_validate(record.kwargs["token"])
        state = TaskState.PENDING if record.status == TaskStatus.PENDING else TaskState.RUNNING
        output: dict[str, Any] | None = None
        result_key: str | None = None
        result_queue = result_queue_key(task_id)
        result_item = await self._db.queue(queue_key(result_queue)).peek()
        if result_item is not None:
            result = ExecutionResultMessage.model_validate(result_item.data)
            output = dict(result.payload)
            state = TaskState.COMPLETED
            result_key = result_queue
        logger.info("runtime status task=%s state=%s", task_id, state.value)
        return TaskEntry(
            task_id=task_id,
            owner=cast(str, record.kwargs.get("client_id")),
            token=token,
            state=state,
            output=output,
            result_key=result_key,
        )

    # --- class lifecycle -------------------------------------------------------

    async def create_class(
        self, spec: AgentSpec, *, spec_yaml: str | None = None, pool: int = 1
    ) -> None:
        """Create a class (§5.2): record, cache the spec, gate at birth, then the
        pool. The class is born enabled iff it has a pool and its dependencies
        are satisfied; composite dependencies are the flattened branch steps.
        ``spec_yaml`` feeds the runtime YAML cache when provided (§4.7)."""
        name = spec.name
        if await self.registry.class_state(name) is not None:
            logger.warning("runtime create_class noop class=%s (already exists)", name)
            return
        dependencies: list[str] = []
        if spec.type is AgentType.COMPOSITE:
            for branch in spec.branches or []:
                dependencies.extend(branch)
            dependencies = sorted(set(dependencies))
        await self._gates.set(name, {"state": ActivityState.DISABLED.value})
        await self.registry.record_class(
            name,
            spec.kind,
            dependencies=dependencies,
            queue=queue_key(name),
            state=ActivityState.DISABLED,
        )
        if spec_yaml is not None:
            await self.registry.cache_spec(name, spec_yaml)
        born_enabled = pool > 0 and await self.registry.dependencies_satisfied(name)
        if pool > 0:
            for _ in range(pool):
                await self._bring_up(
                    name, ActivityState.ENABLED if born_enabled else ActivityState.DISABLED
                )
        if born_enabled:
            await self._gates.set(name, {"state": ActivityState.ENABLED.value})
            await self.registry.set_class_state(name, ActivityState.ENABLED)
        logger.info(
            "runtime create_class class=%s kind=%s pool=%d born=%s",
            name,
            spec.kind.value if spec.kind is not None else "composite",
            pool,
            "enabled" if born_enabled else "disabled",
        )

    async def recreate_class(self, name: str, *, pool: int = 1) -> None:
        """Create a class again from its cached spec after destruction (§5.9).

        The class must not exist; the cached YAML must be present and must
        parse into the class spec (a composite whose referenced patterns are
        absent surfaces the loader's ``UnrecoverableError`` — the constraints
        of a standalone re-parse).
        """
        if await self.registry.class_state(name) is not None:
            logger.warning(
                "runtime recreate_class deny class=%s (already exists)", name
            )
            raise RecoverableError(f"class {name!r} exists; create did already (no-op)")
        spec_yaml = await self.registry.get_cached_spec(name)
        if spec_yaml is None:
            logger.warning("runtime recreate_class deny class=%s (no cached spec)", name)
            raise RecoverableError(f"no cached spec for unknown class {name!r}")
        catalog = load_patterns(spec_yaml)
        spec = catalog.specs.get(name)
        if spec is None:
            logger.warning(
                "runtime recreate_class deny class=%s (cached spec has no such class)",
                name,
            )
            raise RecoverableError(f"cached spec for {name!r} does not define it")
        await self.create_class(spec, spec_yaml=spec_yaml, pool=pool)
        logger.info("runtime recreate_class class=%s pool=%d", name, pool)

    async def create_from_catalog(
        self,
        catalog: Catalog,
        *,
        pools: PoolsConfig,
        spec_yamls: Mapping[str, str] | None = None,
        pool_override: int | None = None,
    ) -> None:
        """Bring the initial catalog state up in topological order (§8).

        Every **served** class is created leaves-first (§8 step 2), so each
        dependent finds its dependencies already created and enabled and is born
        enabled naturally (§4.2/§8 step 4). Each class's pool is the **intent**:
        an explicit ``pool_override`` (the CLI ``--pool N`` of the class-create
        verb, LEG-080) wins over ``pools.resolve(name, kind)`` — that is
        ``per_pattern > per_kind > default`` — which wins over 1. ``0`` borns the
        class disabled (§4.3). ``spec_yamls`` (name → YAML text) feeds the
        runtime YAML cache; absent entries cache nothing yet stay created.

        A dependency **cycle** among the served specs is rejected visibly
        (**before** anything is recorded — rule 9). Anything whose dependencies
        are not satisfied stays disabled and visible (§8 step 5). Unknown YAML
        entries in ``spec_yamls`` are ignored (only served specs create).
        """
        order = self._catalog_order(catalog)
        yamls = spec_yamls or {}
        for name in order:
            spec = catalog.specs[name]
            pool = self._resolve_pool(pools, spec, pool_override)
            await self.create_class(
                spec,
                spec_yaml=yamls.get(name),
                pool=pool,
            )
        logger.info(
            "runtime create_from_catalog classes=%d order=%s",
            len(order),
            ",".join(order),
        )

    @staticmethod
    def _catalog_order(catalog: Catalog) -> list[str]:
        """Topological order of the served specs, leaves first (§8 step 2).

        Kahn's algorithm: repeatedly emit specs with no remaining unresolved
        dependency among the served set. A cycle leaves the queue stuck and
        raises visibly naming a cyclic class (rule 9).
        """
        served = [name for name in catalog.specs if catalog.is_served(name)]
        remaining: set[str] = set(served)

        def unresolved(name: str) -> set[str]:
            spec = catalog.specs.get(name)
            if spec is None or spec.type is not AgentType.COMPOSITE or not spec.branches:
                return set()
            return {
                step
                for branch in spec.branches
                for step in branch
                if step in remaining
            }

        order: list[str] = []
        while remaining:
            ready = sorted(name for name in remaining if not unresolved(name))
            if not ready:
                stuck = sorted(remaining)
                logger.warning("runtime catalog cycle classes=%s", ",".join(stuck))
                raise RecoverableError(
                    "dependency cycle among served classes: " + ",".join(stuck)
                )
            for name in ready:
                remaining.remove(name)
                order.append(name)
        return order

    @staticmethod
    def _resolve_pool(
        pools: PoolsConfig, spec: AgentSpec, pool_override: int | None
    ) -> int:
        """Class-create pool (§4.3/§8): ``pool_override`` > config > 1."""
        if pool_override is not None:
            return pool_override
        resolved = pools.resolve(spec.name, per_kind=spec.kind)
        return 1 if resolved is None else resolved

    async def enable_class(self, name: str) -> None:
        """Enable a class (§5.4): bring an instance up if none exists, order every
        instance enabled (a deposit of ``enable`` at control priority), open the
        gate and record the enabled state. Class enable is the only class-level
        verb that touches instance rows — the faithful translation of the legacy
        "resume every instance" (disable keeps instances draining, policy A)."""
        if await self.registry.class_state(name) is None:
            raise KeyError(f"unknown class {name!r}")
        if await self.registry.class_state(name) == ActivityState.ENABLED:
            logger.warning("runtime enable_class noop class=%s (already enabled)", name)
            return
        if not await self.registry.list_instances(name):
            await self.create_instance(name, count=1)
        for instance in await self.registry.list_instances(name):
            await self.manager.submit_task(
                ENABLE_INSTANCE,
                name,
                instance.instance_id,
                action=ControlAction.ENABLE.value,
            )
            await self._await_instance_state(
                name,
                instance.instance_id,
                ActivityState.ENABLED,
                what=f"enable instance class={name} instance={instance.instance_id}",
            )
        await self._gates.set(name, {"state": ActivityState.ENABLED.value})
        await self.registry.set_class_state(name, ActivityState.ENABLED)
        logger.info("runtime enable_class class=%s", name)

    async def disable_class(self, name: str) -> None:
        """Disable a class (§5.5, policy A: instances keep draining): close the
        gate, record the disabled state, then cascade to every transitive
        dependent. Instance rows are untouched."""
        if await self.registry.class_state(name) is None:
            raise KeyError(f"unknown class {name!r}")
        if await self.registry.class_state(name) == ActivityState.DISABLED:
            logger.warning("runtime disable_class noop class=%s (already disabled)", name)
            return
        await self._gates.set(name, {"state": ActivityState.DISABLED.value})
        await self.registry.set_class_state(name, ActivityState.DISABLED)
        for dependent in await self.registry.class_dependents(name, transitive=True):
            if await self.registry.class_state(dependent) is not None:
                await self._gates.set(dependent, {"state": ActivityState.DISABLED.value})
                await self.registry.set_class_state(dependent, ActivityState.DISABLED)
        logger.info("runtime disable_class class=%s", name)

    async def destroy_class(self, name: str, *, mode: Literal["drain", "now"] = "drain") -> None:
        """Destroy a class (§5.8): close the gate, wait its queue to drain (or
        skip), destroy every instance, clear the queue, drop the gate row and
        the class, then cascade the dependents. Drained classes must be
        recreated via ``recreate_class`` from the cached spec, which survives.
        The drain budgets come from the ``lifecycle`` config (§5.8/§10.2).
        """
        if await self.registry.class_state(name) is None:
            logger.warning("runtime destroy_class noop class=%s (unknown)", name)
            return
        prior_gate = await self._gates.fetch(name)
        await self._gates.set(name, {"state": ActivityState.DISABLED.value})
        if mode == "drain":
            await self._await_queue_empty(name, prior_gate=prior_gate)
        elif mode == "now":
            pass
        else:
            raise ValueError(f"unknown destroy mode {mode!r}")
        for instance in await self.registry.list_instances(name):
            await self.destroy_instance(name, instance.instance_id)
        await self._clear_queue(name)
        await self._gates.delete(name)
        dependents = await self.registry.class_dependents(name, transitive=True)
        await self.registry.remove_class(name)
        for dependent in dependents:
            if await self.registry.class_state(dependent) is not None:
                await self._gates.set(dependent, {"state": ActivityState.DISABLED.value})
                await self.registry.set_class_state(dependent, ActivityState.DISABLED)
        logger.info("runtime destroy_class class=%s mode=%s", name, mode)

    async def _await_queue_empty(self, name: str, *, prior_gate: dict[str, str] | None) -> None:
        """Human-scale polling for the queue to drain (the one scheduled wait in
        the runtime, rule 8 exception documented in §5.8/§10.2). On expiry the
        gate is restored to its pre-destroy value (absent row = open, §12.5.3)."""
        timeout, interval = await self._lifecycle_budget(name)
        queue = self._db.queue(queue_key(name))
        waited = 0.0
        while await queue.count() > 0:
            if waited >= timeout:
                if prior_gate is None:
                    await self._gates.delete(name)
                else:
                    await self._gates.set(name, prior_gate)
                raise RecoverableError(
                    f"drain of class {name!r} timed out after {timeout}s; "
                    "class left untouched (gate restored)"
                )
            await asyncio.sleep(interval)
            waited += interval
        logger.info("runtime drain_done class=%s waited=%.2fs", name, waited)

    async def _clear_queue(self, name: str) -> None:
        """Drain-and-discard the class queue (beaver has no queue deletion)."""
        queue = self._db.queue(queue_key(name))
        while True:
            try:
                await queue.get(block=False)
            except IndexError:
                return

    # --- instance lifecycle ----------------------------------------------------

    async def create_instance(self, name: str, *, count: int = 1) -> list[str]:
        """Create one or more instances of an existing class. Each instance is
        born disabled if the class is disabled, enabled otherwise (§5.1)."""
        if await self.registry.class_state(name) is None:
            raise KeyError(f"unknown class {name!r}")
        born_state = await self.registry.class_state(name) or ActivityState.DISABLED
        created: list[str] = []
        for _ in range(count):
            instance_id = await self._bring_up(name, born_state)
            created.append(instance_id)
        logger.info(
            "runtime create_instance class=%s count=%d born=%s",
            name,
            count,
            born_state.value,
        )
        return created

    async def enable_instance(self, name: str, instance_id: str) -> str:
        """Enable one instance (§5.4) with a report-convergent confirm (LEG-095).

        Deposit an ``enable`` control message on its class queue, then confirm by
        **convergence on the agent's own honor report**: the intake applies the
        ``ready`` report to the Registry state (single writer, no optimistic
        write here) and this verb read-polls the Registry until it reads
        ``ENABLED``. Returns the bring-up task id (unchanged signature).
        """
        instance = await self._require_instance(name, instance_id)
        task_id = self._require_bring_up_task(name, instance_id)
        if instance.state == ActivityState.ENABLED:
            logger.warning(
                "runtime enable_instance noop class=%s instance=%s (already enabled)",
                name,
                instance_id,
            )
            return task_id
        await self.manager.submit_task(
            ENABLE_INSTANCE, name, instance_id, action=ControlAction.ENABLE.value
        )
        await self._await_instance_state(
            name,
            instance_id,
            ActivityState.ENABLED,
            what=f"enable instance class={name} instance={instance_id}",
        )
        logger.info(
            "runtime enable_instance class=%s instance=%s report_converged=True",
            name,
            instance_id,
        )
        return task_id

    async def disable_instance(self, name: str, instance_id: str) -> str:
        """Disable one instance (§5.5) with a report-convergent confirm (LEG-095).

        Deposit a ``disable`` control message on its class queue (the agent parks
        its own loop between dispatches), then confirm by convergence on the
        agent's honor report: the intake applies the ``parked`` report to the
        Registry state (single writer, no optimistic write here) and this verb
        read-polls the Registry until it reads ``DISABLED``. Returns the
        bring-up task id (unchanged signature).
        """
        instance = await self._require_instance(name, instance_id)
        task_id = self._require_bring_up_task(name, instance_id)
        if instance.state == ActivityState.DISABLED:
            logger.warning(
                "runtime disable_instance noop class=%s instance=%s (already disabled)",
                name,
                instance_id,
            )
            return task_id
        await self.manager.submit_task(
            DISABLE_INSTANCE, name, instance_id, action=ControlAction.DISABLE.value
        )
        await self._await_instance_state(
            name,
            instance_id,
            ActivityState.DISABLED,
            what=f"disable instance class={name} instance={instance_id}",
        )
        logger.info(
            "runtime disable_instance class=%s instance=%s report_converged=True",
            name,
            instance_id,
        )
        return task_id

    async def destroy_instance(self, name: str, instance_id: str) -> None:
        """Destroy one instance (§5.7): deposit a ``terminate_with_drain``
        control message on its class queue and confirm the **bring-up** record
        reaches ``success`` — the structural terminal of the agent's exit (the
        message ends the agent sequentially; no ``manager.cancel`` anywhere on
        this path). Destroying the last instance leaves the class disabled but
        existing.

        The vehicle is only knowable in-process (§4.8); a destroy on a legacy
        row with no in-process task (reboot) is a pure Registry fact removal —
        the Manager holds no vehicle to reach (rule 13). The agent's
        ``terminating`` honor report is **informational** (LEG-095): the
        structural record is the confirm; here the pending mint ledger is purged
        and a drained run lets the intake log/consume it.
        """
        instance = await self.registry.get_instance(name, instance_id)
        if instance is None:
            logger.warning(
                "runtime destroy_instance noop class=%s instance=%s", name, instance_id
            )
            return
        task_id = self._instance_tasks.get((name, instance_id))
        if task_id is not None:
            await self.manager.submit_task(
                DESTROY_INSTANCE,
                name,
                instance_id,
                action=ControlAction.TERMINATE_WITH_DRAIN.value,
            )
            await self._await_observable_state(
                task_id,
                lambda record: record.status == TaskStatus.SUCCESS,
                what=f"destroy instance class={name} instance={instance_id}",
                class_name=name,
            )
            self._instance_tasks.pop((name, instance_id), None)
        else:
            logger.info(
                "runtime destroy_instance no_vehicle class=%s instance=%s "
                "(no in-process task; cancel is a structural no-op — legacy fact)",
                name,
                instance_id,
            )
        self._purge_pending(instance_id)
        await self.registry.remove_instance(name, instance_id)
        await self.manager.submit_task(STATE_REPORT_TASK)  # drain the terminating report
        if not await self.registry.list_instances(name):
            await self.registry.set_class_state(name, ActivityState.DISABLED)
        logger.info("runtime destroy_instance class=%s instance=%s", name, instance_id)

    # --- helpers ---------------------------------------------------------------

    def _require_bring_up_task(self, name: str, instance_id: str) -> str:
        task_id = self._instance_tasks.get((name, instance_id))
        if task_id is None:
            raise RecoverableError(
                f"no knowable bring-up task for class={name} instance={instance_id}; "
                "re-boot re-binds it"
            )
        return task_id

    def _purge_pending(self, instance_id: str) -> None:
        """Drop the pending-mint ledger entries of a destroyed instance.

        After a structural destroy the vehicle is gone: its not-yet-consumed
        honoring entries can never produce a valid application (the instance was
        removed) — they are purged visibly, keeping the ledger bounded. Any
        implementing report arriving later is an orphan ``WARNING``, never
        applied (LEG-095 §C).
        """
        stale = [key for key in self._pending_controls if key[0] == instance_id]
        for key in stale:
            del self._pending_controls[key]
        if stale:
            logger.info(
                "runtime pending purged instance=%s entries=%s",
                instance_id,
                len(stale),
            )

    async def _require_instance(self, name: str, instance_id: str) -> InstanceRecord:
        instance = await self.registry.get_instance(name, instance_id)
        if instance is None:
            raise KeyError(f"unknown instance {instance_id!r} of class {name!r}")
        return instance

    # --- read delegations ------------------------------------------------------

    async def list_classes(self) -> list[ClassRecord]:
        """Live catalog classes (Registry read)."""
        return await self.registry.list_classes()

    async def class_state(self, name: str) -> ActivityState | None:
        """Effective activity state of a class (Registry read)."""
        return await self.registry.class_state(name)

    async def class_dependencies(self, name: str) -> list[str]:
        """Declared dependencies of a class (Registry read)."""
        return await self.registry.class_dependencies(name)

    async def class_dependents(self, name: str, *, transitive: bool = False) -> list[str]:
        """Classes depending (possibly transitively) on a class (Registry read)."""
        return await self.registry.class_dependents(name, transitive=transitive)

    async def dependencies_satisfied(self, name: str) -> bool:
        """Whether every declared dependency is currently enabled (Registry read)."""
        return await self.registry.dependencies_satisfied(name)

    async def list_instances(self, class_name: str) -> list[InstanceRecord]:
        """Live instances of a class (Registry read)."""
        return await self.registry.list_instances(class_name)

    async def get_instance(self, class_name: str, instance_id: str) -> InstanceRecord | None:
        """One live instance (Registry read)."""
        return await self.registry.get_instance(class_name, instance_id)

    async def get_cached_spec(self, name: str) -> str | None:
        """The class spec YAML cached at create time (Registry read)."""
        return await self.registry.get_cached_spec(name)

    async def class_kind(self, name: str) -> AgentKind | None:
        """The class's Schema 1 atomic kind (Registry read); ``None`` if unknown/composite."""
        return await self.registry.class_kind(name)


__all__ = [
    "BRING_UP_TASK",
    "CLASS_OP_VERBS",
    "DESTROY_INSTANCE",
    "DISABLE_INSTANCE",
    "ENABLE_INSTANCE",
    "INSTANCE_OP_VERBS",
    "NODE_OPS_SCOPE",
    "NODE_OP_TASK",
    "NODE_OP_VERBS",
    "SEED_TASK",
    "STATE_REPORT_TASK",
    "NodeOp",
    "Runtime",
    "TaskEntry",
    "TaskState",
    "WorkItemReceipt",
]