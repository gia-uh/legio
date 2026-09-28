"""LEG-117 — composite references must not depend on file load order.

A composite whose branch references another composite must load even when the
referencing file sorts before the referenced one (the loader used to validate
each file as it was read, so this failed with "references unknown pattern").
"""

from __future__ import annotations

import pathlib

from legio.patterns import load_pattern_dirs
from legio.patterns.loader import validate_pattern_dirs

LEAF = """
name: leaf
type: atomic
kind: linguistic
input:
  input_as: x
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
    required: [text]
output:
  output_as: out
  output_type: json
  output_schema:
    type: object
    properties:
      result: {type: string}
    required: [result]
prompt: "Do {text}"
"""

INNER = """
name: zzz_inner
type: composite
input:
  input_as: x
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
    required: [text]
output:
  output_as: inner
  output_type: json
  output_schema:
    type: object
    properties:
      out: {type: object}
    required: [out]
branches:
  - [leaf]
"""

OUTER = """
name: aaa_outer
type: composite
main: true
input:
  input_as: x
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
    required: [text]
output:
  output_as: outer
  output_type: json
  output_schema:
    type: object
    properties:
      inner: {type: object}
    required: [inner]
branches:
  - [zzz_inner]
"""


def _tree(tmp_path: pathlib.Path) -> dict[str, pathlib.Path]:
    tool = tmp_path / "tool"
    linguistic = tmp_path / "linguistic"
    composite = tmp_path / "composite"
    for directory in (tool, linguistic, composite):
        directory.mkdir()
    (linguistic / "leaf.yaml").write_text(LEAF, encoding="utf-8")
    # The referencing composite sorts BEFORE the referenced one on purpose.
    (composite / "aaa_outer.yaml").write_text(OUTER, encoding="utf-8")
    (composite / "zzz_inner.yaml").write_text(INNER, encoding="utf-8")
    return {"tool": tool, "linguistic": linguistic, "composite": composite}


def test_forward_composite_reference_loads(tmp_path: pathlib.Path) -> None:
    catalog = load_pattern_dirs(_tree(tmp_path))
    assert "aaa_outer" in catalog.specs
    assert "zzz_inner" in catalog.specs


def test_dry_run_accepts_the_forward_reference(tmp_path: pathlib.Path) -> None:
    assert validate_pattern_dirs(_tree(tmp_path)) == []
