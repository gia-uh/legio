"""LEG-106 — the idle executor does not spin (red-first contract tests).

An idle node's executor pump must park on a real bounded wait instead of a
tight ``asyncio.sleep(0)`` loop. The test counts *empty* ``manager.run()``
passes over a fixed idle window: with real parking the count is bounded by the
number of bounded waits (window / interval), not by the number of loop
iterations the event loop could run.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from legio.cli import executor_loop

IDLE_WINDOW_SECONDS = 0.5


class CountingManager:
    """The slice of `Manager` the pump touches, counting passes."""

    def __init__(self) -> None:
        self.empty_passes = 0
        self.total_passes = 0

    async def run(self) -> int:
        self.total_passes += 1
        self.empty_passes += 1
        return 0


@dataclass(frozen=True)
class _RuntimeLike:
    """The slice of the Runtime the executor pump touches (manager only)."""

    manager: CountingManager


def _pump(runtime: _RuntimeLike, stop: asyncio.Event) -> asyncio.Task:
    return asyncio.create_task(executor_loop(runtime, stop.is_set))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_idle_executor_parks_instead_of_spinning() -> None:
    counter = CountingManager()
    stop = asyncio.Event()
    pump = _pump(_RuntimeLike(manager=counter), stop)
    await asyncio.sleep(IDLE_WINDOW_SECONDS)
    stop.set()
    await asyncio.wait_for(pump, timeout=5)

    # With a tight `sleep(0)` loop this would be hundreds of thousands of
    # passes; with a bounded park it is roughly window / interval.
    assert counter.total_passes < 50, counter.total_passes
    assert counter.empty_passes == counter.total_passes


@pytest.mark.asyncio
async def test_executor_pump_stops_promptly_when_stop_is_set() -> None:
    counter = CountingManager()
    stop = asyncio.Event()
    pump = _pump(_RuntimeLike(manager=counter), stop)
    await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(pump, timeout=5)  # returns, never hangs
