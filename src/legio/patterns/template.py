"""`legio.patterns.template` — dotted-path template resolution (H2 / LEG-031).

Resolves ``{dotted.path}``-style references against the payload plus a set of
system variables (e.g. ``{current_date}``).

Per LEG-031 an **undefined path (or one whose value is ``None``) is an explicit
error, never silent**: it raises ``TemplateResolutionError`` instead of
substituting an empty string, so authoring bugs surface rather than corrupting
a prompt.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from legio.errors import TemplateResolutionError

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][\w.]*)\}")


def resolve_template(
    template: str, payload: Mapping[str, Any], system_vars: Mapping[str, Any]
) -> str:
    """Replace every ``{dotted.path}`` placeholder with its resolved value.

    Raises ``TemplateResolutionError`` if a referenced dotted path does not
    resolve on the payload (undefined key, or a value that is ``None``), rather
    than silently emitting an empty string.
    """

    def _lookup(path: str) -> str:
        if path in system_vars:
            return str(system_vars[path])
        value: Any = payload
        for part in path.split("."):
            if isinstance(value, Mapping):
                value = value.get(part)
            else:
                raise TemplateResolutionError(f"template path {path!r} is undefined on the payload")
        if value is None:
            raise TemplateResolutionError(f"template path {path!r} resolved to None on the payload")
        return str(value)

    return _PLACEHOLDER.sub(lambda m: _lookup(m.group(1)), template)


__all__ = ["resolve_template"]
