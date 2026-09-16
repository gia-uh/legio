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


class _AsyncDouble:
    """Domain-free async callable instance: awaited like an async function."""

    async def __call__(self, text: str) -> dict:
        return {"transformed": str(text).upper()}


async_double = _AsyncDouble()


def fake_returning_coroutine(text: str):  # type: ignore[no-untyped-def]
    """Domain-free sync shape handing back a coroutine: must be awaited."""
    async def _inner() -> dict:
        return {"transformed": str(text).upper()}
    return _inner()


def fake_sync_generator(text: str):  # type: ignore[no-untyped-def]
    """Domain-free sync generator: not a valid tool shape, must fail loudly."""
    yield {"transformed": str(text).upper()}


def fake_returning_asyncgen(text: str):  # type: ignore[no-untyped-def]
    """Domain-free sync shape handing back an async generator: must fail."""
    async def _gen():  # type: ignore[no-untyped-def]
        yield {"transformed": str(text).upper()}
    return _gen()


def fake_thread_probe() -> dict:
    """Domain-free probe reporting the executing thread (off-loop check)."""
    import threading

    return {"thread": threading.current_thread().name}
