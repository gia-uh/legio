"""`legio.errors` — the typed error taxonomy (LEG-016).

All legio errors derive from ``LegioError`` and carry a stable ``code`` derived
from the message. The taxonomy separates transient (recoverable) from fatal
(unrecoverable) authoring/validation failures so they fail loudly and
distinctly. Failures are never silent (AGENTS.md rule 9); there is no retry
policy — errors surface as visible results.
"""

from __future__ import annotations

import re


def code(message: str) -> str:
    """Derive a stable, human-readable error code from a message."""
    slug = re.sub(r"[^a-z0-9]+", "_", message.lower()).strip("_")
    if not slug:
        slug = f"err_{abs(hash(message))}"
    return slug


class LegioError(Exception):
    """Base class for every legio error."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    @property
    def code(self) -> str:
        return code(self.message)


class RecoverableError(LegioError):
    """A transient failure that may succeed on a later run."""


class UnrecoverableError(LegioError):
    """A fatal authoring/validation failure that must surface loudly."""


class InvalidNameError(RecoverableError):
    """An identifier violates its naming contract."""


class TemplateResolutionError(UnrecoverableError):
    """A template dotted path does not resolve on the payload (LEG-031).

    A path that points nowhere is an authoring/pattern bug, not a transient
    failure: it must surface explicitly, never silently substitute an empty
    string (AGENTS.md rule 9).
    """


class ConfigError(UnrecoverableError):
    """The configuration is missing, unreadable, or violates the schema.

    Configuration failures are authoring-level and fatal: the node refuses to
    boot on them (fail-fast, AGENTS.md rule 9) and never falls back silently.
    """


class UnknownAgentError(UnrecoverableError):
    """A starting agent is unknown to the served pattern catalog, or not
    entry-capable (``main``) — an invalid request the node must refuse before
    minting anything (LEG-025)."""


def recoverable(error: LegioError) -> bool:
    """Whether the error type is recoverable (a transient failure)."""
    return isinstance(error, RecoverableError)


__all__ = [
    "ConfigError",
    "InvalidNameError",
    "LegioError",
    "RecoverableError",
    "TemplateResolutionError",
    "UnknownAgentError",
    "UnrecoverableError",
    "code",
    "recoverable",
]
