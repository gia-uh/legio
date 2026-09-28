"""LEG-112 — a configured LLM endpoint with no API key boots (local inference).

Red-first: with only ``services.llm`` configured and no ``LEGIO_LLM_API_KEY``,
the default lingo factory used to raise `OpenAIError: Missing credentials`, so a
linguistic node could not boot against a local OpenAI-compatible server.
"""

from __future__ import annotations

import pathlib

import pytest
from beaver import AsyncBeaverDB

from legio.config import LlmConfig, load
from legio.materializer import boot_node, default_lingo_factory

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_default_lingo_factory_builds_without_api_key() -> None:
    client = default_lingo_factory(
        LlmConfig(base_url="http://127.0.0.1:9/v1", model="local-model"), None
    )
    assert client is not None


@pytest.mark.asyncio
async def test_linguistic_node_boots_without_api_key(beaver_db: AsyncBeaverDB) -> None:
    # ``env={}`` guarantees no LEGIO_LLM_API_KEY is seen, whatever the shell has.
    loaded = load(REPO_ROOT / "examples" / "document_processing" / "legio.yaml", env={})
    booted = await boot_node(loaded, db=beaver_db)  # default factory, no injected lingo
    assert "doc_pipeline" in booted.agents
