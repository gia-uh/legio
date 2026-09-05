"""`legio.patterns.compile` — compile declared schemas to strict pydantic contracts.

Compiles a JSON-schema-style v1 ``input_schema``/``output_schema`` into a
validating pydantic model used for the runtime **superset** contract check:
every declared property is required and strictly typed (a schema ``integer``
never accepts ``"3"``), while anything **not** declared passes through
untouched (extras are allowed — the payload "contains" the contract and may
carry more). Unions, arrays, nested objects and recursive ``$ref`` definitions
compile as before.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, create_model

_STRICT_CONFIG = ConfigDict(strict=True, extra="ignore")


def _pytype(schema: Any, defs: Mapping[str, Any], memo: dict[str, Any]) -> Any:
    if isinstance(schema, list):
        non_null = [s for s in schema if s != "null"]
        inner = _pytype({"type": non_null[0]}, defs, memo) if non_null else Any
        return Any if "null" in schema and len(non_null) == 1 else inner | None

    if not isinstance(schema, Mapping):
        return Any

    ref = schema.get("$ref")
    if ref:
        name = ref.split("/")[-1]
        if name not in memo:
            memo[name] = create_model(f"Ref_{name}", __base__=BaseModel, __config__=_STRICT_CONFIG)
            memo[name] = _compile_submodel(defs[name], defs, memo)
        return memo[name]

    stype = schema.get("type")
    if isinstance(stype, list):
        return _pytype(stype, defs, memo)

    if stype == "string":
        return str
    if stype == "integer":
        return int
    if stype == "number":
        return float
    if stype == "boolean":
        return bool
    if stype == "array":
        items = schema.get("items")
        return list[_pytype(items, defs, memo)] if items is not None else list
    if stype == "object":
        return _compile_submodel(schema, defs, memo)
    if stype is None:
        props = schema.get("properties")
        if props is not None:
            return _compile_submodel(schema, defs, memo)
    return Any


def _compile_submodel(
    schema: Mapping[str, Any], defs: Mapping[str, Any], memo: dict[str, Any]
) -> type[BaseModel]:
    props = schema.get("properties") or {}
    fields: dict[str, Any] = {}
    for name, subschema in props.items():
        fields[name] = (_pytype(subschema, defs, memo), ...)
    return create_model("ContractModel", __base__=BaseModel, __config__=_STRICT_CONFIG, **fields)


def compile_schema(schema: Mapping[str, Any]) -> type[BaseModel]:
    """Return a strict pydantic model validating the given v1 schema.

    Every declared property is required and strictly typed; undeclared fields
    are ignored, so a payload that *contains* the declared data (superset)
    validates even when it carries extras. Used for both ``input_schema`` and
    ``output_schema`` at the agent's two edges.
    """
    defs: dict[str, Any] = dict(schema.get("$defs") or {})
    memo: dict[str, Any] = {}
    return _compile_submodel(schema, defs, memo)


def compile_output_schema(schema: dict[str, Any]) -> type[BaseModel]:
    """Backward-compatible alias: compile an ``output_schema`` to a model."""
    return compile_schema(schema)


__all__ = ["compile_output_schema", "compile_schema"]
