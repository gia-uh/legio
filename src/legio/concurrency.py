"""`legio.concurrency` — concurrency semaphores and cooperative drain (LEG-082).

Operational hygiene, engine-side (no consumer domain, no beaver scope):

- ``ConcurrencyCaps`` shares per-tool and per-LLM ``asyncio.Semaphore`` caps
  across every replica of the agents that use the resource. A constrained
  resource **waits** (cooperative ``Semaphore.acquire``), never fails and
  never drops work. That wait is the engine's second documented rule-8
  exception — like the bounded clock waits of §5.8 it is a cooperative wait,
  never a ``sleep`` and never a poll on a scheduling field.
- ``ShutdownGate`` is an ``asyncio.Event``-backed drain flag that vehicles
  (``AgentBase``'s dispatch and ``CompositeAgent``'s two-inlet poll) honour
  **between dispatches, never inside a step** (§12.2): once requested, no new
  item is pulled, while in-flight steps keep running and always deposit their
  result. Setting it never dequeues and never interrupts a step.
- ``install_signal_drain`` wires OS signals (SIGTERM/SIGINT by default) to
  ``gate.request()`` on the running loop — deployment glue, not the engine's
  own loop (the node is brought up by CLI or programmatically, ARCHITECTURE
  §8; the CLI itself is LEG-081, last).
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


class ConcurrencyCaps:
    """Shared per-resource semaphores: one cap object spans every replica.

    Lazy per-tool semaphore creation keeps the footprint to only the tools
    that carry a declared cap. A missing cap is *unbounded* — the current
    engine behavior, never a regression. Invalid caps (``< 1``) are rejected
    loudly at construction (rule 9).
    """

    def __init__(
        self,
        *,
        max_llm: int | None = None,
        tool_limits: Mapping[str, int] | None = None,
    ) -> None:
        self._tool_limits = dict(tool_limits or {})
        for name, cap in self._tool_limits.items():
            if cap < 1:
                raise ValueError(f"tool concurrency cap for {name!r} must be >= 1 (got {cap})")
        if max_llm is not None and max_llm < 1:
            raise ValueError(f"llm concurrency cap must be >= 1 (got {max_llm})")
        self._llm: asyncio.Semaphore | None = (
            asyncio.Semaphore(max_llm) if max_llm is not None else None
        )
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    @asynccontextmanager
    async def tool(self, name: str) -> AsyncIterator[None]:
        """Hold the tool's semaphore for one execution; no-op when unbounded."""
        limit = self._tool_limits.get(name)
        if limit is None:
            yield
            return
        sem = self._semaphores.get(name)
        if sem is None:
            sem = asyncio.Semaphore(limit)
            self._semaphores[name] = sem
        if sem.locked():
            logger.debug("concurrency wait kind=tool name=%s", name)
        async with sem:
            yield

    @asynccontextmanager
    async def llm(self) -> AsyncIterator[None]:
        """Hold the node-wide LLM semaphore for one call; no-op when unbounded."""
        if self._llm is None:
            yield
            return
        if self._llm.locked():
            logger.debug("concurrency wait kind=llm")
        async with self._llm:
            yield


class ShutdownGate:
    """The drain flag vehicles honour between dispatches (§12.2).

    ``request()`` is idempotent and synchronous (a signal handler may call it):
    it only stops the *pulling of new items*. In-flight steps are never
    interrupted and never lose their deposit.
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def draining(self) -> bool:
        """Whether a drain has been requested (new pulls are held)."""
        return self._event.is_set()

    def request(self) -> None:
        """Request the drain: hold new dispatches; in-flight steps finish."""
        if not self._event.is_set():
            logger.info("shutdown gate requested")
            self._event.set()


def install_signal_drain(
    gate: ShutdownGate,
    *,
    loop: asyncio.AbstractEventLoop | None = None,
    signals: Iterable[signal.Signals] = (signal.SIGTERM, signal.SIGINT),
) -> None:
    """Wire OS signals to ``gate.request()`` on the running loop.

    Deployment glue for a node's operator: raises ``NotImplementedError``
    where the loop cannot deliver signals (e.g. non-main thread), exactly as
    ``loop.add_signal_handler`` does.
    """
    loop = loop if loop is not None else asyncio.get_running_loop()
    for sig in signals:
        loop.add_signal_handler(sig, gate.request)
        logger.info("signal drain wired signal=%s", sig.name)


__all__ = ["ConcurrencyCaps", "ShutdownGate", "install_signal_drain"]