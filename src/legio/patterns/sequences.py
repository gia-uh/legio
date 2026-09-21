"""`legio.patterns.sequences` — starting-route derivation (R-4, LEG-044).

Derives the starting route the submit delivers to. The submit is a dumb
delivery point: it only hands the task to the **starting agent** (the chosen
``main``) and never resolves any DAG. That agent — atomic or composite —
concretizes its own sub-DAG at runtime (ARCHITECTURE §3/§6):

- An atomic (tool/linguistic) starts on its own class.
- A composite starts on its own class; the composite runner concretizes the
  fan-out to its branches (AGENT_LIFECYCLE §12.4).
"""

from __future__ import annotations

from legio.errors import UnrecoverableError
from legio.patterns.schema1 import AgentSpec, AgentType


def starting_route(spec: AgentSpec) -> tuple[tuple[str, str], ...]:
    """Return the route the submit delivers to for a starting agent.

    Each element is a ``(class, input_as)`` pair (AGENT_LIFECYCLE §12.1): the
    delivery class and the alias under which that step reads — the re-keying
    information. The submit re-keys the client payload under the first step's
    ``input_as``.

    - A single atomic pattern (tool/linguistic) yields ``((spec.name, input_as),)``.
    - A composite root yields ``((spec.name, input_as),)`` — the submit delivers
      to the composite's own class queue and the composite runner concretizes the
      fan-out to its branches (§12.4).
    """
    if spec.type is AgentType.ATOMIC:
        return ((spec.name, spec.input.input_as),)

    if spec.type is AgentType.COMPOSITE:
        return ((spec.name, spec.input.input_as),)

    raise UnrecoverableError(f"pattern {spec.name!r} has unknown type: {spec.type}")


__all__ = ["starting_route"]
