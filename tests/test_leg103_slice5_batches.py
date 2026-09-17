"""Contract tests for LEG-103 Slice 5 — CLI order, ingestion, layering, clocks.

Batch 5a (CLI): federation token refusal happens before boot (no substrate
side effects), and pattern YAML collection splits documents with the YAML
parser itself, so a ``---`` line inside a literal block never splits.

Batch 5b (layering/hygiene): shared vocabulary lives down in ``naming``
(agents never import the lifecycle layer for it), and the peer-step filter
travels through the explicit ``Runtime`` constructor seam.
"""

from __future__ import annotations

import hashlib
import logging

import httpx
import pytest
import yaml
from beaver import AsyncBeaverDB

from legio.errors import code
from legio.federation import NodeDB
from legio.manager import Manager
from legio.patterns.loader import split_yaml_documents
from legio.patterns.schema1 import (
    AgentSpec,
    AgentType,
    InputContract,
    IOType,
    OutputContract,
)
from legio.runtime import Runtime
from legio.security import ClientTokenStore


def test_yaml_split_ignores_separator_inside_literal_block() -> None:
    text = (
        "name: alpha\n"
        "text: |\n"
        "  hello\n"
        "  ---\n"
        "  world\n"
        "---\n"
        "name: beta\n"
    )
    segments = split_yaml_documents(text)
    assert len(segments) == 2
    first = yaml.safe_load(segments[0])
    assert first["name"] == "alpha"
    assert first["text"] == "hello\n---\nworld\n"
    assert yaml.safe_load(segments[1])["name"] == "beta"


def test_yaml_split_keeps_explicit_document_markers_parseable() -> None:
    text = "---\nname: one\n---\nname: two\n"
    segments = split_yaml_documents(text)
    assert [yaml.safe_load(segment)["name"] for segment in segments] == ["one", "two"]


def test_yaml_split_skips_blank_segments() -> None:
    assert split_yaml_documents("") == []
    assert split_yaml_documents("---\n") == []


def test_activity_state_is_owned_by_naming() -> None:
    """Agents read gate vocabulary from ``naming``, never from the lifecycle
    layer; the registry re-exports the same object for compatibility."""
    from legio import naming, registry

    assert naming.ActivityState is registry.ActivityState
    assert naming.ActivityState.ENABLED.value == "enabled"
    assert naming.ActivityState.DISABLED.value == "disabled"


def _federated_composite_spec(name: str) -> AgentSpec:
    schema = {"type": "object", "properties": {"text": {"type": "string"}}}
    return AgentSpec(
        type=AgentType.COMPOSITE,
        kind=None,
        name=name,
        input=InputContract(input_as=name, input_type=IOType.JSON, input_schema=schema),
        output=OutputContract(output_as=name, output_type=IOType.JSON, output_schema=schema),
        branches=[["peer_step"]],
    )


async def _recorded_dependencies(runtime: Runtime, name: str) -> list[str]:
    classes = await runtime.registry.list_classes()
    return next(record.dependencies for record in classes if record.name == name)


@pytest.mark.asyncio
async def test_runtime_peer_steps_filter_is_explicit(beaver_db: AsyncBeaverDB) -> None:
    """The peer map reaches ``create_class`` through the constructor seam:
    with it, a peer branch step is not a local dependency; without it, the
    same step counts as local (single-node semantics)."""
    explicit = Runtime(beaver_db, node_id="slice5@host", peer_steps={"peer_step": "peer_in"})
    await explicit.create_class(_federated_composite_spec("fed_explicit"), pool=0)
    assert await _recorded_dependencies(explicit, "fed_explicit") == []

    plain = Runtime(beaver_db, node_id="slice5@host")
    await plain.create_class(_federated_composite_spec("fed_plain"), pool=0)
    assert await _recorded_dependencies(plain, "fed_plain") == ["peer_step"]


def test_token_store_logs_grants_and_revocations(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Security-state mutations are observable — without ever logging the
    secret itself."""
    caplog.set_level(logging.INFO, logger="legio.security")
    store = ClientTokenStore()
    store.register("consumer-a", token="s3cr3t", agents=["main"])
    store.revoke("consumer-a")
    text = caplog.text
    assert "consumer-a" in text
    assert "s3cr3t" not in text


@pytest.mark.asyncio
async def test_manager_pause_logs_its_ttl(
    beaver_db: AsyncBeaverDB, caplog: pytest.LogCaptureFixture
) -> None:
    """The pause row carries a TTL, so the set event names it — expiry
    semantics stay documented instead of silent."""
    caplog.set_level(logging.INFO, logger="legio.manager")
    manager = Manager(beaver_db, node_id="slice5@host")
    task_id = await manager.submit_task("probe")
    await manager.pause(task_id)
    assert "ttl=60" in caplog.text


def test_error_code_fallback_is_deterministic() -> None:
    """The empty-slug fallback must be stable across processes (no
    ``abs(hash())`` randomization)."""
    assert code("!!!") == "err_" + hashlib.md5(b"!!!").hexdigest()[:8]
    assert code("boom") == "boom"


@pytest.mark.asyncio
async def test_result_drain_kicks_coalesce(beaver_db: AsyncBeaverDB) -> None:
    """Repeated read-miss kicks while a drain is already scheduled do not
    mint duplicate persistent tasks; once the drain runs, kicking works."""
    runtime = Runtime(beaver_db, node_id="slice5@host")
    await runtime._kick_result_drain("ghost_agent")
    await runtime._kick_result_drain("ghost_agent")
    assert await runtime.manager._pending.count() == 1
    await runtime._result_drain_fact("ghost_agent")
    await runtime._kick_result_drain("ghost_agent")
    assert await runtime.manager._pending.count() == 2


@pytest.mark.asyncio
async def test_proxy_closes_only_the_client_it_owns(
    beaver_db: AsyncBeaverDB,
) -> None:
    """The proxy's lazy deposit client has an owned lifecycle; an injected
    client stays open for its caller."""
    proxy = NodeDB(beaver_db, node_id="slice5@host")
    owned = proxy._ensure_client()
    assert proxy._client is owned
    await proxy.aclose()
    assert proxy._client is None

    injected = httpx.AsyncClient()
    try:
        borrowed = NodeDB(beaver_db, node_id="slice5@host", client=injected)
        await borrowed.aclose()
        assert injected.is_closed is False
    finally:
        await injected.aclose()
