"""`legio.patterns` — Schema 1 patterns schema and loader (LEG-021, over LEG-010).

Patterns are the only place where domain knowledge lives (YAML as data).
This module parses Schema 1 YAML into typed, immutable pydantic models.
"""

from __future__ import annotations

from legio.patterns.loader import (
    load_pattern_dirs,
    load_patterns,
    resolve_branch,
    resolve_composite_branches,
)
from legio.patterns.schema1 import (
    AgentKind,
    AgentSpec,
    AgentType,
    Catalog,
    InputContract,
    IOType,
    OutputContract,
)
from legio.patterns.sequences import starting_route

__all__ = [
    "AgentKind",
    "AgentSpec",
    "AgentType",
    "Catalog",
    "IOType",
    "InputContract",
    "OutputContract",
    "load_pattern_dirs",
    "load_patterns",
    "resolve_branch",
    "resolve_composite_branches",
    "starting_route",
]
