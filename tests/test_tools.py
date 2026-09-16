"""Test tools for LEG-022 ToolAgent tests."""

def fake_transform(text: str, factor: int = 2) -> dict:
    """Domain-free fake tool: plain callable, signature is its contract."""
    return {"transformed": str(text).upper() * factor}


def fake_flip(text: str) -> dict:
    """Domain-free fake tool: plain callable, signature is its contract."""
    return {"flipped": str(text)[::-1]}


def fake_upper(text: str) -> dict:
    """Domain-free fake tool: plain callable, signature is its contract."""
    return {"upper": str(text).upper()}


async def fake_async_transform(text: str, factor: int = 2) -> dict:
    """Domain-free fake async tool: awaited by the agent, never wrapped raw."""
    return {"transformed": str(text).upper() * factor}


def fake_slow_transform(text: str) -> dict:
    """Domain-free slow sync tool: must hit the policy timeout loudly."""
    import time

    time.sleep(0.5)
    return {"transformed": str(text).upper()}


async def fake_slow_async_transform(text: str) -> dict:
    """Domain-free slow async tool: must hit the policy timeout loudly."""
    import asyncio

    await asyncio.sleep(0.5)
    return {"transformed": str(text).upper()}


async def fake_asyncgen_transform(text: str):  # type: ignore[no-untyped-def]
    """Domain-free async generator: not a valid tool shape, must fail loudly."""
    yield {"transformed": str(text).upper()}
