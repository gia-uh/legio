"""Contract tests for LEG-103 Slice 5 — CLI order, ingestion, layering, clocks.

Batch 5a (CLI): federation token refusal happens before boot (no substrate
side effects), and pattern YAML collection splits documents with the YAML
parser itself, so a ``---`` line inside a literal block never splits.

Batch 5b (layering/hygiene): shared vocabulary lives down in ``naming``
(agents never import the lifecycle layer for it), and the peer-step filter
travels through the explicit ``Runtime`` constructor seam.
"""

from __future__ import annotations

import pytest
import yaml
from beaver import AsyncBeaverDB

from legio.patterns.loader import split_yaml_documents
from legio.patterns.schema1 import (
    AgentSpec,
    AgentType,
    InputContract,
    IOType,
    OutputContract,
)
from legio.runtime import Runtime


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
    from legio import naming
    from legio import registry

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
