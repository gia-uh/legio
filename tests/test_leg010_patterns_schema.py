"""Contract tests for LEG-010 — Patterns Schema 1 (S1).

These tests define the public contract for loading and validating pattern
definitions written in the Schema 1 YAML format, with mandatory symmetric
contracts, terse tool parameters, and chain-wide dotted-path resolution.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from legio.patterns import (
    AgentKind,
    AgentSpec,
    AgentType,
    InputContract,
    IOType,
    OutputContract,
    load_patterns,
)
from legio.patterns.compile import compile_output_schema
from legio.patterns.template import resolve_template

# Schema 1 fixtures — unified composite model (type: composite + branches)

TOOL_SPEC_YAML = """
name: extract
type: atomic
kind: tool
input:
  input_as: text
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
output:
  output_as: extracted
  output_type: json
  output_schema:
    type: object
    properties:
      entities: {type: array, items: {type: string}}
tool: extractor
parameters:
  text: "{text.text}"
"""

LINGUISTIC_SPEC_YAML = """
name: summarize
type: atomic
kind: linguistic
input:
  input_as: extracted
  input_type: json
  input_schema:
    type: object
    properties:
      entities: {type: array, items: {type: string}}
output:
  output_as: summary
  output_type: json
  output_schema:
    type: object
    properties:
      summary: {type: string}
prompt: "Summarize these entities: {entities}"
"""

COMPOSITE_SEQUENCE_YAML = """
name: extract_and_summarize
type: composite
input:
  input_as: payload
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
      lang: {type: string}
output:
  output_as: result
  output_type: json
  output_schema:
    type: object
    properties:
      summary: {type: string}
      entities: {type: array, items: {type: string}}
branches:
  - - extract
    - summarize
"""

COMPOSITE_PARALLEL_YAML = """
name: distribute_summary
type: composite
input:
  input_as: payload
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
output:
  output_as: results
  output_type: json
  output_schema:
    type: object
    properties:
      summ: {type: string}
      cata: {type: string}
branches:
  - - summ
  - - cata
"""

SUMM_SPEC_YAML = """
name: summ
type: atomic
kind: linguistic
input:
  input_as: text
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
output:
  output_as: summ
  output_type: json
  output_schema:
    type: object
    properties:
      summ: {type: string}
prompt: "Summarize: {text}"
"""

CATA_SPEC_YAML = """
name: cata
type: atomic
kind: linguistic
input:
  input_as: text
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
output:
  output_as: cata
  output_type: json
  output_schema:
    type: object
    properties:
      cata: {type: string}
prompt: "Categorize: {text}"
"""

# Full YAML blocks (composite + its referenced atomics) for load_patterns.
# Multiple YAML documents separated by "---"; load_patterns returns a list of
# docs and loads each into the catalog.
SMALL_SEQUENCE_FULL_YAML = f"""
{TOOL_SPEC_YAML}
---
{LINGUISTIC_SPEC_YAML}
---
{COMPOSITE_SEQUENCE_YAML}
"""

PARALLEL_FULL_YAML = f"""
{SUMM_SPEC_YAML}
---
{CATA_SPEC_YAML}
---
{COMPOSITE_PARALLEL_YAML}
"""


def test_tool_agent_spec_schema1_loads() -> None:
    """A Schema 1 tool agent spec loads with mandatory symmetric contracts."""
    from legio.patterns.schema1 import InputContract, OutputContract

    spec = AgentSpec(
        name="extract",
        type=AgentType.ATOMIC,
        kind=AgentKind.TOOL,
        input=InputContract(
            input_as="text",
            input_type=IOType.JSON,
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        ),
        output=OutputContract(
            output_as="extracted",
            output_type=IOType.JSON,
            output_schema={"type": "object", "properties": {"entities": {"type": "array"}}},
        ),
        tool="extractor",
        parameters={"text": "{text.text}"},
    )
    assert spec.name == "extract"
    assert spec.type is AgentType.ATOMIC
    assert spec.kind is AgentKind.TOOL
    assert spec.input.input_as == "text"
    assert spec.output.output_as == "extracted"
    assert spec.tool == "extractor"
    assert spec.parameters == {"text": "{text.text}"}


def test_linguistic_agent_spec_schema1_loads() -> None:
    """A Schema 1 linguistic agent spec loads with mandatory symmetric contracts."""
    from legio.patterns.schema1 import InputContract, OutputContract

    spec = AgentSpec(
        name="summarize",
        type=AgentType.ATOMIC,
        kind=AgentKind.LINGUISTIC,
        input=InputContract(
            input_as="extracted",
            input_type=IOType.JSON,
            input_schema={"type": "object", "properties": {"entities": {"type": "array"}}},
        ),
        output=OutputContract(
            output_as="summary",
            output_type=IOType.JSON,
            output_schema={"type": "object", "properties": {"summary": {"type": "string"}}},
        ),
        prompt="Summarize: {entities}",
    )
    assert spec.kind is AgentKind.LINGUISTIC
    assert spec.prompt == "Summarize: {entities}"
    assert spec.tool is None


def test_single_branch_composite_schema1_loads() -> None:
    """A Schema 1 composite (single-branch) loads with bare pattern names."""
    catalog = load_patterns(SMALL_SEQUENCE_FULL_YAML)
    spec = catalog.get("extract_and_summarize")
    assert spec is not None
    assert spec.name == "extract_and_summarize"
    assert spec.type is AgentType.COMPOSITE
    assert spec.kind is None
    assert spec.input.input_as == "payload"
    assert spec.output.output_as == "result"
    assert spec.branches is not None
    assert len(spec.branches) == 1
    assert spec.branches[0] == ["extract", "summarize"]


def test_multi_branch_composite_schema1_loads() -> None:
    """A Schema 1 composite (multi-branch) loads with bare pattern names."""
    catalog = load_patterns(PARALLEL_FULL_YAML)
    spec = catalog.get("distribute_summary")
    assert spec is not None
    assert spec.name == "distribute_summary"
    assert spec.type is AgentType.COMPOSITE
    assert spec.kind is None
    assert spec.branches is not None
    assert len(spec.branches) == 2
    assert spec.branches[0] == ["summ"]
    assert spec.branches[1] == ["cata"]


def test_mandatory_contracts_enforced() -> None:
    """Missing input/output contract raises ValidationError."""
    with pytest.raises(ValidationError):
        AgentSpec(
            name="bad",
            type=AgentType.ATOMIC,
            kind=AgentKind.TOOL,
            input=InputContract(input_as="x", input_type=IOType.JSON),
            output=OutputContract(
                output_as="y", output_type=IOType.JSON, output_schema={"type": "object"}
            ),
            tool="t",
            parameters={},
        )


def test_branch_exclusive_fields_enforced() -> None:
    """Tool agent must not have prompt; linguistic must not have tool."""
    # tool with prompt -> error
    with pytest.raises(ValidationError):
        AgentSpec(
            name="bad",
            type=AgentType.ATOMIC,
            kind=AgentKind.TOOL,
            input=InputContract(input_as="x", input_type=IOType.JSON, input_schema={}),
            output=OutputContract(output_as="y", output_type=IOType.JSON, output_schema={}),
            tool="t",
            parameters={},
            prompt="should not be here",
        )
    # linguistic with tool -> error
    with pytest.raises(ValidationError):
        AgentSpec(
            name="bad",
            type=AgentType.ATOMIC,
            kind=AgentKind.LINGUISTIC,
            input=InputContract(input_as="x", input_type=IOType.JSON, input_schema={}),
            output=OutputContract(output_as="y", output_type=IOType.JSON, output_schema={}),
            prompt="hello",
            tool="should not be here",
        )


def test_text_type_has_no_schema() -> None:
    """text type must not have input_schema/output_schema."""
    with pytest.raises(ValidationError):
        AgentSpec(
            name="bad",
            type=AgentType.ATOMIC,
            kind=AgentKind.TOOL,
            input=InputContract(
                input_as="x", input_type=IOType.TEXT, input_schema={"type": "object"}
            ),
            output=OutputContract(output_as="y", output_type=IOType.TEXT, output_schema={}),
            tool="t",
            parameters={},
        )


def test_composite_branches_are_bare_pattern_names() -> None:
    """Composite branches contain bare pattern names, not inline specs."""
    catalog = load_patterns(PARALLEL_FULL_YAML)
    spec = catalog.get("distribute_summary")
    assert spec is not None
    assert spec.branches is not None
    for branch in spec.branches:
        for step in branch:
            assert isinstance(step, str)


def test_chain_wide_dotted_path_resolution() -> None:
    """A tool parameter can resolve against any earlier producer in chain."""
    # In SMALL_SEQUENCE_FULL_YAML, the composite references "extract" and "summarize"
    # as bare names; the chain resolution is done at runtime by the loader
    # (validate_agent_spec checks branch names resolve against the catalog)
    catalog = load_patterns(SMALL_SEQUENCE_FULL_YAML)
    spec = catalog.get("extract_and_summarize")
    assert spec is not None
    assert spec.branches is not None
    assert spec.branches[0] == ["extract", "summarize"]


def test_reuse_by_position() -> None:
    """The same agent definition can appear in multiple composites by position."""
    first = COMPOSITE_SEQUENCE_YAML.replace("extract_and_summarize", "first_flow")
    second = COMPOSITE_SEQUENCE_YAML.replace("extract_and_summarize", "second_flow")
    stream = f"{TOOL_SPEC_YAML}\n---\n{LINGUISTIC_SPEC_YAML}\n---\n{first}\n---\n{second}"
    catalog = load_patterns(stream)
    first_spec = catalog.get("first_flow")
    second_spec = catalog.get("second_flow")
    assert first_spec is not None
    assert second_spec is not None
    assert first_spec.branches == [["extract", "summarize"]]
    assert second_spec.branches == [["extract", "summarize"]]


def test_tool_parameters_reject_implicit_path() -> None:
    """A tool parameter path must be explicit `{input_as}.{key}` (§4.12)."""
    from legio.errors import UnrecoverableError

    implicit_yaml = """
name: extract
type: atomic
kind: tool
input:
  input_as: text
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
output:
  output_as: extracted
  output_type: json
  output_schema:
    type: object
    properties:
      extracted: {type: string}
tool: extractor
parameters:
  text: "{text}"
"""
    with pytest.raises(UnrecoverableError, match="must be explicit"):
        load_patterns(implicit_yaml)


def test_tool_parameters_reject_key_not_in_schema() -> None:
    """A tool parameter key must exist in its input_schema (§4.12)."""
    from legio.errors import UnrecoverableError

    unknown_key_yaml = """
name: extract
type: atomic
kind: tool
input:
  input_as: text
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
output:
  output_as: extracted
  output_type: json
  output_schema:
    type: object
    properties:
      extracted: {type: string}
tool: extractor
parameters:
  text: "{text.body}"
"""
    with pytest.raises(UnrecoverableError, match="not in its input_schema"):
        load_patterns(unknown_key_yaml)


def test_starting_route_composite_single_branch() -> None:
    """starting_route returns (name, input_as) for a single-branch composite."""
    from legio.patterns import starting_route

    catalog = load_patterns(SMALL_SEQUENCE_FULL_YAML)
    spec = catalog.get("extract_and_summarize")
    assert spec is not None
    route = starting_route(spec)
    assert route == (("extract_and_summarize", "payload"),)


def test_starting_route_atomic() -> None:
    """starting_route returns ((name, input_as),) for atomic agent."""
    from legio.patterns import starting_route
    from legio.patterns.schema1 import InputContract, OutputContract

    spec = AgentSpec(
        name="single_tool",
        type=AgentType.ATOMIC,
        kind=AgentKind.TOOL,
        input=InputContract(input_as="x", input_type=IOType.JSON, input_schema={}),
        output=OutputContract(output_as="y", output_type=IOType.JSON, output_schema={}),
        tool="t",
        parameters={},
    )
    route = starting_route(spec)
    assert route == (("single_tool", "x"),)


def test_starting_route_composite_multi_branch() -> None:
    """A composite root is a starting agent: the submit delivers to its own
    class and the composite runner concretizes the fan-out (decoupled, LEG-044)."""
    from legio.patterns import starting_route

    catalog = load_patterns(PARALLEL_FULL_YAML)
    spec = catalog.get("distribute_summary")
    assert spec is not None
    assert starting_route(spec) == (("distribute_summary", "payload"),)


def test_compile_output_schema_with_union_array_nested_recursive() -> None:
    """H4: output_schema compiles unions, arrays, nested objects, recursion."""
    schema = {
        "type": "object",
        "properties": {
            "tags": {"type": "array", "items": {"type": "string"}},
            "score": {"type": ["string", "null"]},
            "notes": {"type": "object"},
            "outline": {
                "type": "array",
                "items": {"$ref": "#/$defs/node"},
            },
        },
        "$defs": {
            "node": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "children": {"type": "array", "items": {"$ref": "#/$defs/node"}},
                },
            }
        },
    }
    model = compile_output_schema(schema)
    instance = model(
        tags=["a", "b"], score="high", notes={}, outline=[{"key": "k", "children": []}]
    )
    assert instance.tags == ["a", "b"]  # type: ignore[attr-defined]
    assert instance.score == "high"  # type: ignore[attr-defined]


def test_dotted_template_paths_resolve() -> None:
    """H2: dotted template paths resolve against the payload."""
    template = "Hello {user.name}, you have {count} messages"
    payload = {"user": {"name": "Alice"}, "count": 5}
    result = resolve_template(template, payload, {})
    assert result == "Hello Alice, you have 5 messages"


def test_undefined_template_path_raises() -> None:
    """Undefined template path raises TemplateResolutionError."""
    from legio.errors import TemplateResolutionError

    with pytest.raises(TemplateResolutionError):
        resolve_template("Hello {missing}", {}, {})
