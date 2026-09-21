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
from pathlib import Path

import pytest
from beaver import AsyncBeaverDB

from legio.patterns import Catalog, load_pattern_dirs
from legio.runtime import Runtime

logger = logging.getLogger("legio.tests.conftest")

NODE_ID = "toplevel@test"

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
"""Repo-root ``examples/`` tree (LEG-100): the example-node single source.

The consumer guide, the walkthrough test and the example tests all load the
same files — drift in a documented example breaks the suite."""


def load_example_node(flow: str) -> Catalog:
    """Load one example node's three pattern dirs into a validated Catalog.

    ``flow`` is the node directory name under ``examples/`` (``transform``,
    ``summarize``, ``extract-and-summarize``, ``distribute-summary``).
    """
    node = EXAMPLES / flow
    return load_pattern_dirs(
        {
            "tool": node / "patterns" / "tool",
            "linguistic": node / "patterns" / "linguistic",
            "composite": node / "patterns" / "composite",
        }
    )


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
