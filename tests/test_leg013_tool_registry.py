"""Contract tests for LEG-013 — Schema 3 tool registry (available_tools).

These tests pin the public contract for the node's tool registry: tools are
declared in `available_tools: {<name>: {implementation, policy}}`. Each tool is
a callable loaded from its dotted path at execution time. The tool's signature
(via `inspect`) is its contract; the tool does not declare pydantic schemas.
"""

from __future__ import annotations

import pytest

from legio.tools import (
    AvailableToolsRegistry,
    Tool,
    resolve_parameters,
    validate_callable_signature,
)


def fake_transform(text: str, factor: int = 2) -> dict:
    """Domain-free fake tool: plain callable, signature is its contract."""
    return {"transformed": str(text).upper() * factor}


def test_tool_is_just_a_callable() -> None:
    """A tool is a plain callable; it does not expose pydantic schemas."""
    assert callable(fake_transform)
    assert not hasattr(fake_transform, "input_schema")
    assert not hasattr(fake_transform, "output_schema")


def test_fake_tool_conforms_to_tool_protocol() -> None:
    assert isinstance(fake_transform, Tool)


def test_declare_and_load_tool() -> None:
    registry = AvailableToolsRegistry()
    registry.declare(
        "transform",
        implementation="tests.test_tools.fake_transform",
        policy={"timeout": 30, "retries": 0},
    )

    tool = registry.load_tool("transform")
    assert callable(tool)
    # The tool is the function from test_tools.py
    from tests.test_tools import fake_transform as fake_transform_fn

    # tool is loaded as a callable; assert same code object
    assert tool.__code__ is fake_transform_fn.__code__  # type: ignore[attr-defined]


def test_declare_all_declarations() -> None:
    registry = AvailableToolsRegistry()
    registry.declare(
        "transform",
        implementation="tests.test_tools.fake_transform",
        policy={"timeout": 30, "retries": 0},
    )
    registry.declare(
        "other",
        implementation="tests.test_tools.fake_flip",
        policy={"timeout": 10, "retries": 1},
    )

    decls = registry.all_declarations()
    assert "transform" in decls
    assert "other" in decls
    assert decls["transform"]["implementation"] == "tests.test_tools.fake_transform"
    assert decls["transform"]["policy"] == {"timeout": 30, "retries": 0}


def test_load_unknown_tool_raises() -> None:
    registry = AvailableToolsRegistry()
    with pytest.raises(KeyError):
        registry.load_tool("no_such_tool")


def test_get_declaration_unknown_tool_raises() -> None:
    registry = AvailableToolsRegistry()
    with pytest.raises(KeyError):
        registry.get_declaration("no_such_tool")


def test_resolve_parameters_with_dotted_paths() -> None:
    payload = {"text": "hello", "factor": 2, "nested": {"value": 42}}
    parameters = {"text": "{text}", "factor": "{factor}", "nested_val": "{nested.value}"}
    resolved = resolve_parameters(parameters, payload)
    assert resolved["text"] == "hello"
    assert resolved["factor"] == 2
    assert resolved["nested_val"] == 42


def test_resolve_parameters_with_literals() -> None:
    payload = {"text": "hello"}
    parameters = {"text": "{text}", "factor": 5, "flag": True}
    resolved = resolve_parameters(parameters, payload)
    assert resolved["text"] == "hello"
    assert resolved["factor"] == 5
    assert resolved["flag"] is True


def test_resolve_parameters_missing_path_raises() -> None:
    payload = {"text": "hello"}
    parameters = {"text": "{missing}"}
    with pytest.raises(KeyError):
        resolve_parameters(parameters, payload)


def test_resolve_parameters_none_value_raises() -> None:
    payload = {"text": None}
    parameters = {"text": "{text}"}
    with pytest.raises(KeyError):
        resolve_parameters(parameters, payload)


def test_validate_callable_signature_ok() -> None:
    validate_callable_signature(fake_transform, {"text": "hello", "factor": 2})


def test_validate_callable_signature_missing_required_raises() -> None:
    with pytest.raises(TypeError):
        validate_callable_signature(fake_transform, {"factor": 2})


def test_validate_callable_signature_unexpected_kwarg_raises() -> None:
    with pytest.raises(TypeError):
        validate_callable_signature(fake_transform, {"text": "hello", "unknown": 123})
