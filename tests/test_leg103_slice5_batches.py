"""Contract tests for LEG-103 Slice 5 — CLI order, ingestion, layering, clocks.

Batch 5a (CLI): federation token refusal happens before boot (no substrate
side effects), and pattern YAML collection splits documents with the YAML
parser itself, so a ``---`` line inside a literal block never splits.
"""

from __future__ import annotations

import yaml

from legio.patterns.loader import split_yaml_documents


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
