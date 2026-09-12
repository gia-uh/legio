"""Shared test fixtures over native beaver.

Every test that needs the substrate connects a fresh local beaver SQLite
database (a temp file) and yields that single ``AsyncBeaverDB`` — the same
decoupling a multi-process deployment gets by opening the same file. No
invented substrate layer exists."""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import AsyncIterator

import pytest
from beaver import AsyncBeaverDB

from legio.runtime import Runtime

logger = logging.getLogger("legio.tests.conftest")

NODE_ID = "toplevel@test"


@pytest.fixture
async def beaver_db() -> AsyncIterator[AsyncBeaverDB]:
    """Connect a fresh temp beaver db; yield the shared connection."""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "test.db")
    db = AsyncBeaverDB(path)
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
def runtime(beaver_db: AsyncBeaverDB) -> Runtime:
    """A Runtime over the shared beaver db (LEG-085 public face)."""
    return Runtime(beaver_db, node_id=NODE_ID)
