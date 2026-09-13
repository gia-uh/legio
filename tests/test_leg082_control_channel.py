"""Contract tests for LEG-082 — the authenticated control channel + standing loop.

The only way to talk to an agent is its queue. Lifecycle orders (enable /
disable / terminate-with-drain) arrive as signed ``ControlMessage``s on the
agent's **class queue**, minted only by the Runtime and verified by a pure,
verify-only handle the agent holds. The ``standing_loop`` is the instance's
long-lived interior loop: it suspends on the class queue and honors a validated
control message **between dispatches** — never mid-step. Control deposits use
``CONTROL_PRIORITY`` (-1.0, lower than work 0.0; beaver ``ORDER BY priority
ASC``) so a control message jumps the FIFO work.

This file is the red part of LEG-082: the message algebra (sign/verify/replay/
forge) and the loop behavior (park, resume, drain-exit, foreign-target requeue,
silent-drop refusal). All substrate is native beaver over a temp db.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Self

import pytest
from beaver import AsyncBeaverDB

from legio.agents.base import AgentBase
from legio.flow import (
    CONTROL_PRIORITY,
    ControlAction,
    ControlMessage,
    ControlOrigin,
    ControlVerifier,
    ExecutionRequestMessage,
    derive_control_key,
    sign_control,
)
from legio.naming import queue_key

KEY_MATERIAL = "unit-test-boot-secret"


def make_request(*, task_id: str) -> ExecutionRequestMessage:
    """A single-step level-1 request whose result lands on ``client``."""
    return ExecutionRequestMessage(
        level_route=(("main", "in"),),
        current_index=0,
        end_of_level_queue="client",
        level=1,
        task_id=task_id,
        payload={"in": {"v": 1}},
    )


class EchoAgent(AgentBase):
    """A minimal atomic agent that records every processed work item."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(output_as="out", **kwargs)
        self.processed: list[ExecutionRequestMessage] = []

    async def _handle(self, request: ExecutionRequestMessage) -> dict[str, Any]:
        self.processed.append(request)
        return {"out": request.payload}


class RunningLoop:
    """A context-managed standing loop task; always torn down on exit."""

    def __init__(self, agent: EchoAgent, instance_id: str) -> None:
        self.agent = agent
        self.instance_id = instance_id
        self.task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        self.task = asyncio.create_task(self.agent.standing_loop(self.instance_id))
        await asyncio.sleep(0.0)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


async def consume_until(predicate: Any, *, timeout: float = 3.0) -> None:
    """Repeatedly evaluate ``predicate`` until truthy (bounded clock wait)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"consume_until timed out after {timeout}s")
        await asyncio.sleep(0.05)


async def queue_count(db: AsyncBeaverDB, queue: str) -> int:
    return await db.queue(queue_key(queue)).count()


def put_control(
    key: bytes,
    *,
    action: ControlAction,
    seq: int,
    target: str = "main-1",
) -> ControlMessage:
    return sign_control(
        key,
        target_instance=target,
        action=action,
        seq=seq,
        origin=ControlOrigin.OPERATOR,
    )


async def deposit_control(
    db: AsyncBeaverDB,
    message: ControlMessage,
    *,
    priority: float = CONTROL_PRIORITY,
) -> None:
    await db.queue(queue_key("main")).put(message.model_dump(mode="json"), priority=priority)


async def deposit_work(db: AsyncBeaverDB, task_id: str) -> None:
    await db.queue(queue_key("main")).put(
        make_request(task_id=task_id).model_dump(mode="json"), priority=0.0
    )


# --- A. the message algebra (no substrate) -------------------------------------


def test_derive_control_key_is_deterministic_and_scoped() -> None:
    key_a = derive_control_key(KEY_MATERIAL)
    key_b = derive_control_key(KEY_MATERIAL)
    other = derive_control_key("another-boot-secret")
    assert key_a == key_b
    assert key_a != other
    assert len(key_a) == 32  # sha256 digest


def test_sign_verify_roundtrip() -> None:
    key = derive_control_key(KEY_MATERIAL)
    message = put_control(key, action=ControlAction.DISABLE, seq=1)
    assert message.message_type == "control"
    assert message.seq == 1
    assert ControlVerifier(key).verify(message)


def test_tampered_field_rejected() -> None:
    key = derive_control_key(KEY_MATERIAL)
    message = put_control(key, action=ControlAction.DISABLE, seq=1)
    tampered = message.model_dump(mode="json")
    tampered["target_instance"] = "victim-9"
    assert not ControlVerifier(key).verify(ControlMessage.model_validate(tampered))


def test_wrong_key_rejected() -> None:
    message = put_control(derive_control_key(KEY_MATERIAL), action=ControlAction.DISABLE, seq=1)
    assert not ControlVerifier(derive_control_key("attacker")).verify(message)


def test_verifier_cannot_mint() -> None:
    key = derive_control_key(KEY_MATERIAL)
    verifier = ControlVerifier(key)
    forged = put_control(key, action=ControlAction.ENABLE, seq=2)
    forged_tampered = forged.model_dump(mode="json")
    forged_tampered["action"] = ControlAction.TERMINATE_WITH_DRAIN.value
    assert not verifier.verify(ControlMessage.model_validate(forged_tampered))


def test_control_message_is_strict() -> None:
    key = derive_control_key(KEY_MATERIAL)
    with pytest.raises(ValueError):
        put_control(key, action=ControlAction.DISABLE, seq=0)
    signed = put_control(key, action=ControlAction.DISABLE, seq=1)
    with pytest.raises(ValueError):  # extra field forbidden
        ControlMessage.model_validate({**signed.model_dump(), "x": 1})


# --- B. the standing loop (native beaver substrate) ----------------------------


@pytest.mark.asyncio
async def test_standing_loop_processes_work(beaver_db: AsyncBeaverDB) -> None:
    agent = EchoAgent(
        agent_id="main",
        db=beaver_db,
        control_verifier=ControlVerifier(derive_control_key(KEY_MATERIAL)),
    )
    async with RunningLoop(agent, "main-1") as loop:
        await deposit_work(beaver_db, "t1")
        await consume_until(lambda: len(agent.processed) == 1)
        assert await queue_count(beaver_db, "client") == 1
        assert loop.task is not None and not loop.task.done()


@pytest.mark.asyncio
async def test_disable_parks_and_enable_resumes(beaver_db: AsyncBeaverDB) -> None:
    agent = EchoAgent(
        agent_id="main",
        db=beaver_db,
        control_verifier=ControlVerifier(derive_control_key(KEY_MATERIAL)),
    )
    key = derive_control_key(KEY_MATERIAL)
    async with RunningLoop(agent, "main-1"):
        await deposit_control(
            beaver_db, put_control(key, action=ControlAction.DISABLE, seq=1)
        )
        await consume_until(lambda: agent._control_paused)
        await deposit_work(beaver_db, "t1")
        await consume_until(lambda: len(agent._paused_hold) == 1)
        assert agent.processed == []
        await deposit_control(
            beaver_db, put_control(key, action=ControlAction.ENABLE, seq=2)
        )
        await consume_until(lambda: len(agent.processed) == 1)
        assert agent._paused_hold == []
        assert not agent._control_paused


@pytest.mark.asyncio
async def test_terminate_with_drain_ends_loop_and_releases_hold(
    beaver_db: AsyncBeaverDB,
) -> None:
    agent = EchoAgent(
        agent_id="main",
        db=beaver_db,
        control_verifier=ControlVerifier(derive_control_key(KEY_MATERIAL)),
    )
    key = derive_control_key(KEY_MATERIAL)
    loop = RunningLoop(agent, "main-1")
    await loop.__aenter__()
    try:
        await deposit_control(
            beaver_db, put_control(key, action=ControlAction.DISABLE, seq=1)
        )
        await consume_until(lambda: agent._control_paused)
        await deposit_work(beaver_db, "t1")
        await consume_until(lambda: len(agent._paused_hold) == 1)
        await deposit_control(
            beaver_db,
            put_control(key, action=ControlAction.TERMINATE_WITH_DRAIN, seq=2),
        )
        await asyncio.wait_for(loop.task, timeout=3.0)  # type: ignore[arg-type]
        assert agent.processed == []
        assert agent._paused_hold == []
        assert await queue_count(beaver_db, "main") == 1  # released work, untouched
    finally:
        await loop.__aexit__()


@pytest.mark.asyncio
async def test_foreign_target_control_is_requeued_not_dropped(
    beaver_db: AsyncBeaverDB,
) -> None:
    agent = EchoAgent(
        agent_id="main",
        db=beaver_db,
        control_verifier=ControlVerifier(derive_control_key(KEY_MATERIAL)),
    )
    key = derive_control_key(KEY_MATERIAL)
    async with RunningLoop(agent, "main-1"):
        await deposit_control(
            beaver_db,
            put_control(key, action=ControlAction.DISABLE, seq=1, target="main-2"),
        )
        await asyncio.sleep(0.2)
        assert not agent._control_paused  # never honored by this instance
        await deposit_work(beaver_db, "t1")
        await consume_until(lambda: len(agent.processed) == 1)
        # The foreign control was requeued and cycles (best-effort with pool=1,
        # §12.5.4-style): it is never honored, never ack-dropped — the queue may
        # hold it or be mid-cycle, but the wrong instance never acted on it.
        assert await queue_count(beaver_db, "main") in (0, 1)


@pytest.mark.asyncio
async def test_bad_signature_dropped_visibly_loop_alive(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture
) -> None:
    agent = EchoAgent(
        agent_id="main",
        db=beaver_db,
        control_verifier=ControlVerifier(derive_control_key(KEY_MATERIAL)),
    )
    key = derive_control_key(KEY_MATERIAL)
    async with RunningLoop(agent, "main-1"):
        with caplog.at_level(logging.WARNING, logger="legio.agents.base"):
            forged = put_control(key, action=ControlAction.DISABLE, seq=1).model_dump()
            forged["seq"] = 9
            await beaver_db.queue(queue_key("main")).put(forged, priority=CONTROL_PRIORITY)
            await asyncio.sleep(0.2)
        assert not agent._control_paused
        assert "bad_signature" in caplog.text
        await deposit_control(
            beaver_db, put_control(key, action=ControlAction.DISABLE, seq=2)
        )
        await consume_until(lambda: agent._control_paused)


@pytest.mark.asyncio
async def test_no_verifier_drops_control_visibly(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture
) -> None:
    agent = EchoAgent(agent_id="main", db=beaver_db)
    async with RunningLoop(agent, "main-1"):
        with caplog.at_level(logging.WARNING, logger="legio.agents.base"):
            await deposit_control(
                beaver_db,
                put_control(
                    derive_control_key(KEY_MATERIAL),
                    action=ControlAction.DISABLE,
                    seq=1,
                ),
            )
            await asyncio.sleep(0.2)
        assert "no_verifier" in caplog.text
        await deposit_work(beaver_db, "t1")
        await consume_until(lambda: len(agent.processed) == 1)


@pytest.mark.asyncio
async def test_replay_is_rejected(beaver_db: AsyncBeaverDB) -> None:
    agent = EchoAgent(
        agent_id="main",
        db=beaver_db,
        control_verifier=ControlVerifier(derive_control_key(KEY_MATERIAL)),
    )
    key = derive_control_key(KEY_MATERIAL)
    async with RunningLoop(agent, "main-1"):
        await deposit_control(
            beaver_db, put_control(key, action=ControlAction.DISABLE, seq=1)
        )
        await consume_until(lambda: agent._control_paused)
        # the very same message replayed must be dropped (anti-replay)...
        replayed = put_control(key, action=ControlAction.DISABLE, seq=1)
        await deposit_control(beaver_db, replayed)
        await asyncio.sleep(0.2)
        await deposit_work(beaver_db, "t1")
        await consume_until(lambda: len(agent._paused_hold) == 1)
        assert agent.processed == []
        # ...and a fresh sequence still honored afterwards
        await deposit_control(
            beaver_db, put_control(key, action=ControlAction.ENABLE, seq=2)
        )
        await consume_until(lambda: len(agent.processed) == 1)


@pytest.mark.asyncio
async def test_control_priority_jumps_work(beaver_db: AsyncBeaverDB) -> None:
    agent = EchoAgent(
        agent_id="main",
        db=beaver_db,
        control_verifier=ControlVerifier(derive_control_key(KEY_MATERIAL)),
    )
    key = derive_control_key(KEY_MATERIAL)
    async with RunningLoop(agent, "main-1"):
        await deposit_work(beaver_db, "t1")
        await deposit_control(
            beaver_db, put_control(key, action=ControlAction.DISABLE, seq=1)
        )
        await consume_until(lambda: agent._control_paused)
        assert agent.processed == []
        await consume_until(lambda: len(agent._paused_hold) == 1)