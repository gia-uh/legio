"""Test-only tool implementations for resilience scenarios."""

import asyncio
from collections.abc import Mapping
from typing import Any


async def slow_tool(duration: float = 0.2) -> Mapping[str, Any]:
    """A tool that sleeps for `duration` seconds. Used to test timeouts."""
    await asyncio.sleep(duration)
    return {"slept": duration}


def failing_tool() -> Mapping[str, Any]:
    """A tool that always raises an exception. Used to test error handling."""
    raise RuntimeError("intentional failure for testing")
