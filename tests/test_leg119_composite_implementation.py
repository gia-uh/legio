"""LEG-119 — a composite may declare its own implementation (declarative).

A particular composition (not the built-in default) is supplied by declaring
``implementation:`` on the composite: a dotted path to a ``CompositeAgent``
subclass, resolved normally or **node-local** beside the node config. The
programmatic ``composite_classes`` map still wins; absent everything, the engine
default applies.
"""

from __future__ import annotations

import pathlib

import pytest
from beaver import AsyncBeaverDB

from legio.agents import CompositeAgent
from legio.errors import ConfigError, UnrecoverableError
from legio.materializer import load_composite_class
from legio.patterns import load_pattern_dirs, load_patterns

METRICS = """
name: metrics
type: composite
implementation: "metrics_impl.SpeakerCount"
input:
  input_as: transcript
  input_type: json
  input_schema:
    type: object
    properties:
      transcription: {type: string}
      language: {type: string}
    required: [transcription, language]
output:
  output_as: metrics
  output_type: json
  output_schema:
    type: object
    properties:
      speakers:
        type: array
        items: {type: string}
    required: [speakers]
    additionalProperties: false
branches:
  - [text]
"""

LEAF = """
name: text
type: atomic
kind: linguistic
input:
  input_as: transcript
  input_type: json
  input_schema:
    type: object
    properties:
      transcription: {type: string}
      language: {type: string}
    required: [transcription, language]
output:
  output_as: text
  output_type: json
  output_schema:
    type: object
    properties:
      text: {type: string}
    required: [text]
    additionalProperties: false
prompt: "repeat {transcription} in {language}"
"""

METRICS_IMPL = """\
from legio.agents import CompositeAgent
from legio.flow import build_payload


class SpeakerCount(CompositeAgent):
    async def build_output_as(self, info, own=None):
        speakers = []
        for payload in info.values():
            for item in payload.get("interventions", {}).get("interventions", []) or []:
                speakers.append(item.get("speaker"))
        return build_payload(
            {"speakers": sorted({s for s in speakers if s})},
            output_as=self._output_as,
        )
"""


def _write_tree(tmp_path: pathlib.Path) -> dict[str, pathlib.Path]:
    for directory in ("tool", "linguistic", "composite"):
        (tmp_path / directory).mkdir()
    (tmp_path / "composite" / "metrics.yaml").write_text(METRICS, encoding="utf-8")
    (tmp_path / "linguistic" / "text.yaml").write_text(LEAF, encoding="utf-8")
    (tmp_path / "metrics_impl.py").write_text(METRICS_IMPL, encoding="utf-8")
    return {
        "tool": tmp_path / "tool",
        "linguistic": tmp_path / "linguistic",
        "composite": tmp_path / "composite",
    }


def test_declared_implementation_parses(tmp_path: pathlib.Path) -> None:
    catalog = load_pattern_dirs(_write_tree(tmp_path))
    assert catalog.specs["metrics"].implementation == "metrics_impl.SpeakerCount"


def test_declared_implementation_loads_node_local(tmp_path: pathlib.Path) -> None:
    _write_tree(tmp_path)
    cls = load_composite_class("metrics_impl.SpeakerCount", base_dir=tmp_path)
    assert issubclass(cls, CompositeAgent)
    assert cls.__name__ == "SpeakerCount"


def test_broken_implementation_fails_loudly(tmp_path: pathlib.Path) -> None:
    _write_tree(tmp_path)
    with pytest.raises(ConfigError):
        load_composite_class("metrics_impl.Nope", base_dir=tmp_path)
    with pytest.raises(ConfigError):
        load_composite_class("not_a_module.Class", base_dir=tmp_path)


def test_implementation_on_atomic_is_rejected() -> None:
    yaml = """
name: t
type: atomic
kind: linguistic
implementation: "x.Y"
input:
  input_as: x
  input_type: json
  input_schema: {type: object, properties: {text: {type: string}}}
output:
  output_as: o
  output_type: json
  output_schema: {type: object, properties: {r: {type: string}}}
prompt: "do {text}"
"""
    with pytest.raises(UnrecoverableError):
        load_patterns(yaml)


@pytest.mark.asyncio
async def test_declared_class_builds_its_own_output(beaver_db: AsyncBeaverDB) -> None:
    """The declared class's build_output_as runs (observed through its output)."""
    composite = _inline_class()(
        agent_id="metrics",
        db=beaver_db,
        branches=((("interventions", "transcript"),),),
        input_as="transcript",
        output_as="metrics",
        input_schema={"type": "object", "properties": {"transcription": {"type": "string"}}},
        output_schema={"type": "object", "properties": {"speakers": {"type": "array"}}},
    )
    built = await composite.build_output_as(
        {
            "b1": {
                "interventions": {
                    "interventions": [
                        {"statement": "a", "speaker": "Ana"},
                        {"statement": "b", "speaker": "Luis"},
                        {"statement": "c", "speaker": "Ana"},
                    ]
                }
            }
        }
    )
    assert built == {"metrics": {"speakers": ["Ana", "Luis"]}}


def _inline_class() -> type[CompositeAgent]:
    from legio.flow import build_payload

    class SpeakerCount(CompositeAgent):
        async def build_output_as(self, info, own=None):  # type: ignore[override]
            speakers = []
            for payload in info.values():
                for item in payload.get("interventions", {}).get("interventions", []) or []:
                    speakers.append(item.get("speaker"))
            return build_payload(
                {"speakers": sorted({s for s in speakers if s})},
                output_as=self._output_as,
            )

    return SpeakerCount
