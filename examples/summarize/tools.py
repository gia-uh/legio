"""Node-local tool implementation for the ``summarize`` example.

Declared by dotted path ``tools.assess`` (LEG-104): node-local tools resolve
beside the ``tools.yaml`` that declares them.
"""

from __future__ import annotations


def assess(title: str, summary: str) -> dict:
    """Domain-free tool: consumes a structured ``{title, summary}`` pair."""
    return {"result": f"[{title}] {summary}"}
