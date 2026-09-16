"""`legio.patterns.loader` — Schema 1 patterns loader (LEG-021).

Loads and validates patterns from YAML Schema 1 into typed AgentSpec models,
with all LEG-010 validations: branch-exclusive, mandatory contracts,
chain-wide dotted-path resolution, contract compatibility, reuse, encapsulation.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from legio.errors import UnrecoverableError
from legio.patterns.schema1 import AgentSpec, Catalog

logger = logging.getLogger(__name__)


def _validate_tool_parameters(spec: AgentSpec) -> None:
    """Validate a tool's terse `parameters` (AGENT_LIFECYCLE §4.12).

    Each dotted path must be **explicit** `{input_as}.{key}` — the agent's own
    declared `input_as` followed by a key of its `input_schema`. No implicit
    resolution, no other prefix. A literal needs no producer.
    """
    input_as = spec.input.input_as
    schema_keys = (
        set(spec.input.input_schema.get("properties", {}).keys())
        if spec.input.input_schema
        else set()
    )
    for arg, value in (spec.parameters or {}).items():
        if not (isinstance(value, str) and value.startswith("{") and value.endswith("}")):
            continue  # literal
        path = value[1:-1]
        parts = path.split(".")
        if len(parts) != 2 or parts[0] != input_as:
            raise UnrecoverableError(
                f"tool agent {spec.name!r}: parameter {arg!r} path {path!r} must be "
                f"explicit '{{input_as}}.{{key}}' with input_as={input_as!r}"
            )
        key = parts[1]
        if key not in schema_keys:
            raise UnrecoverableError(
                f"tool agent {spec.name!r}: parameter {arg!r} key {key!r} is not in "
                f"its input_schema"
            )


def _validate_agent_spec(
    spec: AgentSpec,
    parent_scope: dict[str, Any] | None = None,
    catalog: dict[str, AgentSpec] | None = None,
    peer_steps: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Validate a single agent spec and return its output scope for children.

    Raises UnrecoverableError on any validation failure.
    """
    if catalog is None:
        catalog = {}

    # Interior ↔ contract coherence
    if spec.kind is not None and spec.kind.value == "tool":
        # terse parameters must be explicit `{input_as}.{key}` (§4.12)
        _validate_tool_parameters(spec)
        # Tool-signature coherence stays execution-time only (LEG-013 two
        # verification domains; LEG-103 Slice 5e): the tool loads dynamically,
        # so its contract is not verifiable at load.

    elif spec.kind is not None and spec.kind.value == "linguistic" and spec.prompt:
        # prompt variables ↔ input_schema
        import re

        vars_in_prompt = set(re.findall(r"\{([a-zA-Z_][\w.]*)\}", spec.prompt))
        input_schema_props = (
            spec.input.input_schema.get("properties", {}) if spec.input.input_schema else {}
        )
        declared = set(input_schema_props.keys())
        unused = vars_in_prompt - declared - {"current_date"}
        undeclared = declared - vars_in_prompt
        if unused:
            raise UnrecoverableError(
                f"linguistic agent {spec.name!r}: prompt uses undeclared variables: {unused}"
            )
        if undeclared:
            raise UnrecoverableError(
                f"linguistic agent {spec.name!r}: input_schema has unused declarations: {undeclared}"
            )

    # Composite: validate branches (bare pattern names resolved against catalog).
    # Composite branch steps are resolved against the local catalog ∪ any
    # **peer roster** (LEG-090 amended by LEG-094): a step the local catalog
    # does not serve may be a *known peer step* (the ``peer_steps`` map the
    # boot derives from the fetched rosters, federation.roster_steps) — it is
    # let through the load gate as ``(step_name, peer_input_as)``, never
    # widening scope beyond a peer that genuinely offers it (rule 9).
    if spec.type.value == "composite" and spec.branches:
        for branch_idx, branch in enumerate(spec.branches):
            for step_name in branch:
                if step_name in catalog:
                    continue
                if peer_steps is not None and step_name in peer_steps:
                    continue
                raise UnrecoverableError(
                    f"composite {spec.name!r} branch {branch_idx} "
                    f"references unknown pattern: {step_name!r}"
                )
                # Tool step parameters are validated autonomously per spec
                # (explicit `{input_as}.{key}` on the step's own contracts) —
                # no cross-sibling scope is needed for load validation.

    if spec.output.output_schema:
        return {spec.output.output_as: spec.output.output_schema.get("properties", {})}
    return {}


def resolve_branch(
    branch: list[str],
    catalog: Catalog,
    peer_steps: Mapping[str, str] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Resolve a composite branch (bare pattern names) to a ``(class, input_as)`` route.

    Each step is a reference to a defined agent; its ``(class, input_as)`` pair is
    the class name and the referenced agent's declared ``input_as`` (Schema 2 —
    resolved by the loader when it builds the level/branch DAG, never declared on
    the step). A step whose referenced agent is a ``type: composite`` is kept as
    a position in the route; that composite performs its own fan-out when invoked
    through its inbox (reuse by reference, recursion).

    Federation (LEG-094 session 86e): a step the local catalog does not serve may
    reference an agent a configured **peer** offers (LEG-090 rosters, amended by
    session 86e). ``peer_steps`` is the ``step name → input_as`` map the boot
    derives from the fetched rosters (federation.roster_steps): the step routes
    as ``(step_name, peer_input_as)`` — the executing peer agent reads the
    message re-keyed under its own declared ``input_as``, exactly as this node's
    own agent would (wire parity, rule 13). A step named by neither the local
    catalog nor any peer roster is the same visible reference error as today —
    federation never widens scope, it only lets a *known peer step* through the
    load gate (rule 9).
    """
    route: list[tuple[str, str]] = []
    for step_name in branch:
        if step_name in catalog:
            if catalog.is_invalid(step_name):
                raise UnrecoverableError(
                    f"branch references invalid pattern: {step_name!r} (not served)"
                )
            step = catalog.specs[step_name]
            route.append((step.name, step.input.input_as))
            continue
        if peer_steps is not None and step_name in peer_steps:
            route.append((step_name, peer_steps[step_name]))
            continue
        raise UnrecoverableError(
            f"branch references unknown pattern: {step_name!r}"
        )
    return tuple(route)


def resolve_composite_branches(
    spec: AgentSpec,
    catalog: Catalog,
    peer_steps: Mapping[str, str] | None = None,
) -> list[tuple[tuple[str, str], ...]]:
    """Resolve every branch of a composite to its expanded ``(class, input_as)`` route.

    Threads ``peer_steps`` (step → peer ``input_as``, federation.roster_steps —
    LEG-094/LEG-095 boot seam) into each branch's resolution so a step the
    local catalog does not serve may route to the peer that offers it (wire
    parity, rule 13; never widening scope, rule 9).
    """
    if spec.type.value != "composite" or not spec.branches:
        raise UnrecoverableError(f"spec {spec.name!r} is not a composite with branches")
    return [resolve_branch(branch, catalog, peer_steps=peer_steps) for branch in spec.branches]


def _load_specs_from_yaml(
    data: Any, catalog: Catalog, peer_steps: Mapping[str, str] | None = None
) -> list[AgentSpec]:
    """Load one or more specs from parsed YAML data."""
    if isinstance(data, dict):
        docs = [data]
    elif isinstance(data, list):
        docs = data
    else:
        raise UnrecoverableError("YAML must be a dict or list of dicts")

    specs = []
    for doc in docs:
        if not isinstance(doc, dict):
            raise UnrecoverableError("each pattern must be a mapping")
        spec = AgentSpec(**doc)
        specs.append(spec)
        if spec.name in catalog.specs:
            raise UnrecoverableError(f"duplicate pattern name: {spec.name}")
        catalog.specs[spec.name] = spec

    # Second pass: validate with full catalog for reuse references
    for spec in specs:
        _validate_agent_spec(
            spec, catalog=catalog.specs, peer_steps=peer_steps
        )

    return specs


def load_pattern_dirs(
    pattern_dirs: Mapping[str, Path], *, peer_steps: Mapping[str, str] | None = None
) -> Catalog:
    """Load patterns from the three per-type directories (recursive ``*.yaml``).

    Each directory is scanned recursively (``rglob``) and every document is
    loaded into one accumulated Catalog (duplicate names across dirs are a load
    error). A configured directory that is missing is a visible, loud boot
    failure (rule 9) — never a silent empty scan.

    Federation (LEG-094 session 86e): ``peer_steps`` (step → peer ``input_as``,
    the roster map the boot derives — federation.roster_steps) is threaded into
    the load gate so a composite branch may reference a *known peer step*; it
    never widens scope beyond a peer that genuinely offers it (rule 9).
    """
    catalog = Catalog()
    for kind, directory in pattern_dirs.items():
        path = Path(directory)
        if not path.is_dir():
            raise UnrecoverableError(
                f"pattern directory missing: {path} (field {kind!r})"
            )
        for yaml_file in sorted(path.rglob("*.yaml")):
            _load_all_documents(
                yaml_file.read_text(encoding="utf-8"), catalog, peer_steps=peer_steps
            )
        logger.info("patterns loaded kind=%s dir=%s count=%d", kind, path, len(catalog))
    return catalog


def load_patterns(source: str | Path | dict[str, Any] | list[dict[str, Any]]) -> Catalog:
    """Load patterns from a YAML file/directory or dict/list.

    Args:
        source: Path to YAML file/directory, or parsed dict/list, or YAML string.

    Returns:
        A read-only Catalog of validated AgentSpec models.

    Raises:
        UnrecoverableError: on any validation failure (parse, branch-exclusive,
            mandatory contracts, chain resolution, contract compatibility,
            reuse, encapsulation/cycles).
    """
    catalog = Catalog()

    # Handle YAML string (contains newlines or starts with YAML indicators)
    if isinstance(source, str) and ("\n" in source or source.strip().startswith(("{", "[", "-", "name:"))):
        _load_all_documents(source, catalog)
    elif isinstance(source, (str, Path)):
        path = Path(source)
        if path.is_dir():
            for yaml_file in sorted(path.glob("*.yaml")):
                _load_all_documents(yaml_file.read_text(encoding="utf-8"), catalog)
        else:
            _load_all_documents(path.read_text(encoding="utf-8"), catalog)
    elif isinstance(source, (dict, list)):
        _load_specs_from_yaml(source, catalog)
    else:
        raise UnrecoverableError(f"unsupported source type: {type(source)}")

    logger.info("patterns loaded count=%d", len(catalog))
    return catalog


def _load_all_documents(
    text: str, catalog: Catalog, peer_steps: Mapping[str, str] | None = None
) -> None:
    """Load every YAML document in a stream (multi-doc ``---`` supported)."""
    for document in yaml.safe_load_all(text):
        if document is not None:
            _load_specs_from_yaml(document, catalog, peer_steps=peer_steps)


def _strip_separator_lines(segment: str) -> str:
    """Drop document-boundary lines (``---``/``...``) from a segment's ends.

    Only boundary lines are dropped: a separator-looking line inside a literal
    block is always indented, and boundaries detected by the parser land at
    the segment ends by construction.
    """
    lines = segment.splitlines(keepends=True)
    while lines and lines[0].strip() == "---":
        lines.pop(0)
    while lines and lines[-1].strip() in ("---", "..."):
        lines.pop()
    return "".join(lines)


def split_yaml_documents(text: str) -> list[str]:
    """Split a YAML stream into raw per-document segments using the parser.

    Boundaries come from ``yaml.compose_all`` marks — the same parser family
    ``safe_load_all`` uses — so a ``---`` line inside a literal block never
    splits. Each segment parses standalone with ``yaml.safe_load``.
    """
    starts = [document.start_mark.index for document in yaml.compose_all(text)]
    starts.append(len(text))
    segments: list[str] = []
    for start, end in zip(starts, starts[1:]):
        segment = _strip_separator_lines(text[start:end])
        if segment.strip():
            segments.append(segment)
    return segments


__all__ = [
    "Catalog",
    "load_pattern_dirs",
    "load_patterns",
    "resolve_branch",
    "resolve_composite_branches",
    "split_yaml_documents",
]