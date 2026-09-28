"""`legio.tools` — the Schema 3 tool registry (LEG-013).

Tools are declared in `available_tools: {<name>: {implementation, policy}}`.
Each tool is a callable loaded from its dotted path at execution time.
The tool's signature (via `inspect`) is the contract; the tool itself does
not declare pydantic schemas — the consuming agent declares `output_as`/
`output_schema` (Schema 1).
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import logging
import sys
from collections.abc import Mapping
from pathlib import Path
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
    """Registry for Schema 3 `available_tools` declaration.

    ``base_dir`` is the directory of the ``tools.yaml`` that declared the
    tools (the node directory). It is the fallback root for **node-local**
    implementations: a dotted path whose module is not normally importable is
    loaded from ``<base_dir>/<module>.py`` by file location under a unique
    synthetic module name (no ``sys.path`` mutation, no cross-node collision).
    A consumer that installs its tools as a normal package keeps the plain
    dotted path and needs no ``base_dir``.
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self._declarations: dict[str, dict[str, Any]] = {}
        self._base_dir = base_dir
        self._loaded: dict[str, Tool] = {}

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
        """Load and return the tool callable from its dotted path.

        A normal dotted import is tried first; when it fails and a ``base_dir``
        was given, the implementation is resolved as a **node-local** file
        ``<base_dir>/<module>.py``. The loaded callable is cached per registry
        (``load_tool`` runs once per dispatch).
        """
        cached = self._loaded.get(name)
        if cached is not None:
            return cached
        decl = self.get_declaration(name)
        dotted_path = decl["implementation"]
        try:
            module_path, attr = dotted_path.rsplit(".", 1)
            module = self._import_module(module_path)
            tool = getattr(module, attr)
        except (ImportError, AttributeError, ValueError, OSError) as exc:
            logger.error("failed to load tool name=%s path=%s error=%s", name, dotted_path, exc)
            raise UnrecoverableError(f"cannot load tool {name!r} from {dotted_path!r}") from exc
        if not callable(tool):
            logger.error("tool non-callable name=%s path=%s", name, dotted_path)
            raise UnrecoverableError(f"tool {name!r} resolved to non-callable: {tool!r}")
        self._loaded[name] = tool
        return tool

    def _import_module(self, module_path: str) -> Any:
        """Import ``module_path`` normally, or node-local as a fallback."""
        try:
            return importlib.import_module(module_path)
        except ImportError:
            local = self._load_local_module(module_path)
            if local is None:
                raise
            return local

    def _load_local_module(self, module_path: str) -> Any:
        """Load ``<base_dir>/<module_path>.py`` as a synthetic module, or None."""
        if self._base_dir is None:
            return None
        candidate = self._base_dir.joinpath(*module_path.split(".")).with_suffix(".py")
        if not candidate.is_file():
            return None
        digest = hashlib.sha1(str(candidate.resolve()).encode("utf-8")).hexdigest()[:12]
        synthetic = f"_legio_node_tools_{digest}_{module_path.replace('.', '_')}"
        existing = sys.modules.get(synthetic)
        if existing is not None:
            return existing
        spec = importlib.util.spec_from_file_location(synthetic, candidate)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[synthetic] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(synthetic, None)
            raise
        logger.info("tool local import module=%s file=%s", module_path, candidate)
        return module

    def all_declarations(self) -> Mapping[str, dict[str, Any]]:
        """Return all declared tools (read-only view)."""
        return self._declarations


def load_local_module(module_path: str, base_dir: Path | str | None) -> Any | None:
    """Load ``<base_dir>/<module_path>.py`` as a synthetic module, or None (LEG-104).

    Module-level counterpart of ``AvailableToolsRegistry._load_local_module`` so
    other node-local loaders (LEG-119 composite implementations) reuse the exact
    same mechanism: file-location import under a unique synthetic name, no
    ``sys.path`` mutation.
    """
    if base_dir is None:
        return None
    registry = AvailableToolsRegistry(base_dir=Path(base_dir))
    return registry._load_local_module(module_path)


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
