"""`legio.flow.token` — the immutable FlowToken (LEG-011, Schema 2).

The FlowToken is the immutable payload travelling with a route. Finality is
*derived from position* (``is_final(total_steps)`` compares ``current_index``
against the last step), never stored. A root token marks the task as a client
process (``root``); the actual return destination is the submit's
``end_of_level_queue`` (the final-result queue) and lives in the token, never
derived from ``task_id`` — there is no ``client:{task_id}`` family (addenda
AJ/AL).
"""

from __future__ import annotations

from .messages import ImmutableMessage


class FlowToken(ImmutableMessage):
    """Immutable route-travelling token; finality derived from position."""

    root: bool = False

    def is_final(self, total_steps: int) -> bool:
        """Whether this token points at the last step of ``total_steps``.

        Position-only helper: production routing derives finality inline
        (``AgentBase._route_outcome``) as end-of-sequence AND ``level == 1``,
        so this helper is level-blind by design — see ARCH §3.
        """
        return self.current_index == total_steps - 1


__all__ = ["FlowToken"]
