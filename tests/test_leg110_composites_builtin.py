"""LEG-110 (amended) — composites build themselves; a composite node boots.

A composite's construction is **internal to legio** (the branches are in the
YAML): the built-in `CompositeAgent` default merges the branch payloads under
`output_as`. No external class, no `composites.config`, nothing to declare — a
node with composites boots straight from its config.

This file replaces the earlier node-local-composites contract (which required an
injected class per composite); that mechanism was wrong and has been removed.
"""

from __future__ import annotations

import pathlib

import pytest
from beaver import AsyncBeaverDB
from lingo.mock import MockLLM

from legio.agents import CompositeAgent
from legio.config import load
from legio.materializer import boot_node

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPOSITE_EXAMPLES = (
    "summarize",
    "extract-and-summarize",
    "distribute-summary",
    "document_processing",
)
EXAMPLE_COMPOSITES = {
    "summarize": "summarize",
    "extract-and-summarize": "extract_and_summarize",
    "distribute-summary": "distribute_summary",
    "document_processing": "doc_pipeline",
}


def _mock_lingo_factory(_llm: object, _api_key: object) -> MockLLM:
    return MockLLM(responses=[])


@pytest.mark.asyncio
async def test_build_output_as_default_merges_branches(beaver_db: AsyncBeaverDB) -> None:
    """The built-in default merges the branch payloads under output_as."""
    composite = CompositeAgent(
        agent_id="c",
        db=beaver_db,
        branches=((("leaf_a", "x"),), (("leaf_b", "x"),)),
        input_as="in",
        output_as="out",
    )
    result = await composite.build_output_as(
        {"b1": {"summary": {"summary": "s"}}, "b2": {"categories": {"categories": ["t"]}}}
    )
    assert result == {"out": {"summary": {"summary": "s"}, "categories": {"categories": ["t"]}}}


@pytest.mark.asyncio
async def test_boot_node_needs_no_composites_config(beaver_db: AsyncBeaverDB) -> None:
    """No injected classes, no config key: the config alone boots the node."""
    loaded = load(REPO_ROOT / "examples" / "summarize" / "legio.yaml")
    booted = await boot_node(loaded, db=beaver_db, lingo_factory=_mock_lingo_factory)
    assert "summarize" in booted.agents
    assert isinstance(booted.agents["summarize"], CompositeAgent)


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", COMPOSITE_EXAMPLES)
async def test_every_composite_example_boots_from_config(
    flow: str, beaver_db: AsyncBeaverDB
) -> None:
    loaded = load(REPO_ROOT / "examples" / flow / "legio.yaml")
    booted = await boot_node(loaded, db=beaver_db, lingo_factory=_mock_lingo_factory)
    composite_name = EXAMPLE_COMPOSITES[flow]
    assert isinstance(booted.agents[composite_name], CompositeAgent)
