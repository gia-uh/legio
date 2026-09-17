"""`legio.patterns.schema1` — Schema 1 agent spec models (LEG-010).

One agent spec: `type` × `kind` with mandatory symmetric contracts
and terse call vocabulary. No v1 legacy fields (`input_mapping`, etc.).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from legio.errors import UnrecoverableError

logger = logging.getLogger(__name__)


class AgentType(str, Enum):
    ATOMIC = "atomic"
    COMPOSITE = "composite"


class AgentKind(str, Enum):
    TOOL = "tool"
    LINGUISTIC = "linguistic"


class IOType(str, Enum):
    TEXT = "text"
    JSON = "json"
    BINARY = "binary"


class InputContract(BaseModel):
    """Mandatory entry contract for every agent."""

    input_as: str = Field(..., description="Read alias (no reserved words)")
    input_type: IOType
    input_schema: dict[str, Any] | None = Field(
        default=None, description="Mandatory for json/binary; absent for text"
    )

    @model_validator(mode="after")
    def _validate_schema_for_type(self) -> InputContract:
        if self.input_type in (IOType.JSON, IOType.BINARY):
            if self.input_schema is None:
                raise ValueError(f"{self.input_type.value} requires input_schema")
        else:  # text
            if self.input_schema is not None:
                raise ValueError("text type must not have input_schema")
        return self


class OutputContract(BaseModel):
    """Mandatory output contract for every agent."""

    output_as: str = Field(..., description="Write alias")
    output_type: IOType
    output_schema: dict[str, Any] | None = Field(
        default=None, description="Mandatory for json/binary; absent for text"
    )

    @model_validator(mode="after")
    def _validate_schema_for_type(self) -> OutputContract:
        if self.output_type in (IOType.JSON, IOType.BINARY):
            if self.output_schema is None:
                raise ValueError(f"{self.output_type.value} requires output_schema")
        else:  # text
            if self.output_schema is not None:
                raise ValueError("text type must not have output_schema")
        return self


class AgentPolicy(BaseModel):
    """One agent's execution policy (Schema 1, every type — NOT tool policy).

    ``timeout`` bounds one handling of one inbox item, enforced by the
    common runner (`AgentBase._run_guarded`); expiry is a visible
    `TimeoutError` result. This is the step layer: tool policy
    (`ToolPolicy`, Schema 3) bounds each *call attempt* inside a tool
    step instead — different layer, different owner, no cross-checks.
    """

    model_config = {"extra": "forbid"}

    timeout: int | float | None = Field(
        default=None,
        description="Seconds bounding one step handling (None = unbounded, declared)",
    )

    @field_validator("timeout", mode="before")
    @classmethod
    def _reject_non_number_timeout(cls, value: object) -> object:
        if isinstance(value, (bool, str)):
            # Pydantic wraps only ValueError into ValidationError.
            raise ValueError(  # noqa: TRY004 - value rejection, naming the bound
                f"pattern policy.timeout must be a genuine number of seconds > 0 (got {value!r})"
            )
        return value

    @model_validator(mode="after")
    def _validate_timeout(self) -> AgentPolicy:
        if self.timeout is not None and (
            not isinstance(self.timeout, (int, float)) or not math.isfinite(self.timeout) or self.timeout <= 0
        ):
            raise ValueError(
                f"pattern policy.timeout must be a finite number of seconds > 0 (got {self.timeout!r})"
            )
        return self


class AgentSpec(BaseModel):
    """One agent spec: type × kind with mandatory symmetric contracts."""

    type: AgentType
    kind: AgentKind | None = Field(
        default=None, description="tool | linguistic (atomic only); None for composite"
    )
    name: str
    description: str | None = None
    main: bool = False

    # Mandatory symmetric contracts
    input: InputContract
    output: OutputContract

    # Interior — ATOMIC only, by kind
    tool: str | None = Field(
        default=None, description="available_tools key (kind: tool)"
    )
    parameters: dict[str, str | int | float | bool] | None = Field(
        default=None, description="Terse call: {arg: dotted.path | literal}"
    )
    prompt: str | None = Field(
        default=None, description="Prompt template (kind: linguistic)"
    )

    # Interior — COMPOSITE only
    branches: list[list[str]] | None = Field(
        default=None,
        description="List of branches; each branch an ordered list of bare pattern names",
    )

    # Execution policy — EVERY type (uniform step bound, enforced by the
    # common runner; distinct from Schema 3 tool policy)
    policy: AgentPolicy | None = Field(
        default=None,
        description="Step execution policy (timeout bounds one inbox-item handling)",
    )

    @model_validator(mode="after")
    def _validate_kind_fields(self) -> AgentSpec:
        if self.type is AgentType.ATOMIC:
            if self.kind is None:
                raise ValueError("atomic requires kind (tool | linguistic)")
            if self.branches is not None:
                raise ValueError("atomic must not have branches")
            if self.kind is AgentKind.TOOL:
                if self.tool is None:
                    raise ValueError("kind: tool requires tool (available_tools key)")
                if self.parameters is None:
                    raise ValueError("kind: tool requires parameters")
                if self.prompt is not None:
                    raise ValueError("kind: tool must not have prompt")
            elif self.kind is AgentKind.LINGUISTIC:
                if self.prompt is None:
                    raise ValueError("kind: linguistic requires prompt")
                if self.tool is not None:
                    raise ValueError("kind: linguistic must not have tool")
                if self.parameters is not None:
                    raise ValueError("kind: linguistic must not have parameters")
            else:
                raise ValueError(f"unknown kind: {self.kind}")
        elif self.type is AgentType.COMPOSITE:
            if self.kind is not None:
                raise ValueError("composite must not have kind")
            if self.branches is None:
                raise ValueError("composite requires branches")
            if self.tool is not None:
                raise ValueError("composite must not have tool")
            if self.prompt is not None:
                raise ValueError("composite must not have prompt")
            if self.parameters is not None:
                raise ValueError("composite must not have parameters")
        else:
            raise ValueError(f"unknown type: {self.type}")

        return self


# Rebuild for forward references
AgentSpec.model_rebuild()


class Catalog(BaseModel):
    """Catalog of loaded agent specs with cascade invalidation (LEG-070).

    The loaded ``specs`` are read-only after load. Invalidation is a separate,
    observable runtime state: ``invalidate`` marks one pattern and, transitively,
    every pattern that depends on it (through composite ``branches``) as invalid;
    invalid patterns are removed from the **served** catalog. The dependency
    graph is a DAG over composite references (reuse by reference; no cycles load,
    LEG-021).
    """

    specs: dict[str, AgentSpec] = Field(default_factory=dict)
    invalid: frozenset[str] = Field(default_factory=frozenset)

    def get(self, name: str) -> AgentSpec | None:
        return self.specs.get(name)

    def values(self) -> Iterable[AgentSpec]:
        return self.specs.values()

    def __contains__(self, name: str) -> bool:
        return name in self.specs

    def __len__(self) -> int:
        return len(self.specs)

    def dependents(self, name: str) -> frozenset[str]:
        """Direct dependents: composites whose branches reference ``name``."""
        return frozenset(
            spec.name
            for spec in self.specs.values()
            if spec.type is AgentType.COMPOSITE
            and spec.branches
            and any(name in branch for branch in spec.branches)
        )

    def invalidate(self, name: str) -> frozenset[str]:
        """Invalidate ``name`` and every dependent transitively.

        Returns the patterns newly invalidated (empty on a re-invalidation —
        idempotent). The catalog never serves invalid patterns afterwards. An
        unknown pattern is a visible error, never silent (rule 9).
        """
        if name not in self.specs:
            raise UnrecoverableError(f"cannot invalidate unknown pattern: {name!r}")

        frontier = {name}
        reachable: set[str] = set()
        while frontier:
            next_frontier: set[str] = set()
            for current in frontier:
                for dependent in self.dependents(current):
                    if dependent not in reachable:
                        reachable.add(dependent)
                        next_frontier.add(dependent)
            frontier = next_frontier
        reachable.add(name)

        newly_invalid = frozenset(reachable) - self.invalid
        if newly_invalid:
            self.invalid = self.invalid | newly_invalid
            logger.warning(
                "patterns invalidated count=%d chain=%s",
                len(newly_invalid),
                ",".join(sorted(newly_invalid)),
            )
        return newly_invalid

    def is_invalid(self, name: str) -> bool:
        """Whether ``name`` is invalidated (disabled, never served)."""
        return name in self.invalid

    def served(self) -> frozenset[str]:
        """The served catalog: every loaded pattern minus the invalid set."""
        return frozenset(name for name in self.specs if name not in self.invalid)

    def is_served(self, name: str) -> bool:
        """Whether ``name`` is loaded and not invalidated (served)."""
        return name in self.specs and name not in self.invalid


__all__ = [
    "AgentKind",
    "AgentSpec",
    "AgentType",
    "Catalog",
    "IOType",
    "InputContract",
    "OutputContract",
]