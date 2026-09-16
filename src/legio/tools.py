"""`legio.tools` — the Schema 3 tool registry (LEG-013).

Tools are declared in `available_tools: {<name>: {implementation, policy}}`.
Each tool is a callable loaded from its dotted path at execution time.
The tool's signature (via `inspect`) is the contract; the tool itself does
not declare pydantic schemas — the consuming agent declares `output_as`/
`output_schema` (Schema 1).
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from legio.errors import UnrecoverableError

logger = logging.getLogger(__name__)


@runtime_checkable
class Tool(Protocol):
    """A substitutable execution resource — a sync or async callable.

    The tool's signature (via `inspect`) is its contract. The tool does
    not expose pydantic schemas; the consuming agent validates I/O.
    Async callables are awaited by the agent; async generators are rejected.
    """

    def __call__(self, *args: Any, **kwargs: Any) -> Any: ...


class AvailableToolsRegistry:
    """Registry for Schema 3 `available_tools` declaration."""

    def __init__(self) -> None:
        self._declarations: dict[str, dict[str, Any]] = {}

    def declare(
        self,
        name: str,
        *,
        implementation: str,
        policy: Mapping[str, Any] | None = None,
    ) -> None:
        """Declare a tool in `available_tools`."""
        if name in self._declarations:
            logger.warning("tool redeclared name=%s", name)
        self._declarations[name] = {
            "implementation": implementation,
            "policy": dict(policy) if policy else {},
        }
        logger.info("tool declared name=%s implementation=%s", name, implementation)

    def get_declaration(self, name: str) -> dict[str, Any]:
        """Return the declaration for `name` (raises KeyError if missing)."""
        try:
            return self._declarations[name]
        except KeyError:
            logger.warning("tool not found name=%s", name)
            raise KeyError(f"no tool declared for name {name!r}") from None

    def load_tool(self, name: str) -> Tool:
        """Load and return the tool callable from its dotted path."""
        decl = self.get_declaration(name)
        dotted_path = decl["implementation"]
        try:
            module_path, attr = dotted_path.rsplit(".", 1)
            module = __import__(module_path, fromlist=[attr])
            tool = getattr(module, attr)
        except (ImportError, AttributeError, ValueError) as exc:
            logger.error("failed to load tool name=%s path=%s error=%s", name, dotted_path, exc)
            raise UnrecoverableError(
                f"cannot load tool {name!r} from {dotted_path!r}"
            ) from exc
        if not callable(tool):
            raise TypeError(f"tool {name!r} resolved to non-callable: {tool!r}")
        return tool

    def all_declarations(self) -> Mapping[str, dict[str, Any]]:
        """Return all declared tools (read-only view)."""
        return self._declarations


def resolve_parameters(
    parameters: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve terse `parameters` against the incoming `payload`.

    Each value in `parameters` is either:
    - a dotted path string starting with `{` and ending with `}` (e.g., `{summ.text}`
      for the agent's own `input_as` plus a key of its `input_schema`)
      → resolved against `payload` via dotted path lookup
    - a literal value (int, str, bool, etc.) → used as-is

    Raises `KeyError` if a dotted path is not found on the payload.
    """
    resolved: dict[str, Any] = {}
    for arg, value in parameters.items():
        if isinstance(value, str) and value.startswith("{") and value.endswith("}"):
            path = value[1:-1]
            current: Any = payload
            for part in path.split("."):
                if isinstance(current, Mapping):
                    current = current.get(part)
                else:
                    raise KeyError(f"parameter path {path!r} is undefined on the payload")
            if current is None:
                raise KeyError(f"parameter path {path!r} resolved to None on the payload")
            resolved[arg] = current
        else:
            resolved[arg] = value
    return resolved


def validate_callable_signature(tool: Tool, kwargs: Mapping[str, Any]) -> None:
    """Validate `kwargs` against the tool's signature at execution time.

    Raises `TypeError` if the signature rejects the call.
    """
    sig = inspect.signature(tool)
    try:
        sig.bind(**kwargs)
    except TypeError as exc:
        raise TypeError(f"tool signature mismatch: {exc}") from exc


__all__ = [
    "AvailableToolsRegistry",
    "Tool",
    "resolve_parameters",
    "validate_callable_signature",
]
