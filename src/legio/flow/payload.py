"""`legio.flow.payload` — payload building (AGENT_LIFECYCLE §12.1).

Each agent receives the incoming ``payload``, reads only what it needs under its
``input_as`` (an alias it owns), performs its processing, and **builds its own
new payload** under its ``output_as``. The payload is **construction, not
accumulation**: ``build_payload`` produces exactly ``{output_as: output}`` — it
never merges or extends the incoming payload, and it never mutates state. The
re-keying (key change) to the next agent's ``input_as`` is the handoff
step's responsibility (AGENT_LIFECYCLE §12.1, Session 20), not this builder's.

``output`` is any produced value, not only a mapping: with no declared
``output_schema`` the default output model is plain text, and the value under
``output_as`` is the string itself — ``{output_as: "the_text"}``, never a
nested ``{text: ...}`` wrapper (the default text output model). The pattern's
``build_output_as`` implementation decides which value it wraps here.
"""

from __future__ import annotations

from typing import Any

__all__ = ["build_payload"]


def build_payload(output: Any, *, output_as: str) -> dict[str, Any]:
    """Build the agent's new payload, constructed under its ``output_as``.

    ``output`` is the step's produced value; ``output_as`` is the agent's
    declared write alias (Schema 1). The result is a brand-new dict with a
    single key — ``{output_as: <output>}``. There is no union, no accumulation,
    and the incoming payload is never extended or mutated.
    """
    return {output_as: output}
