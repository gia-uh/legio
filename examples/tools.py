"""Reference tool implementations for the example nodes (domain-free).

Every example node's ``tools.yaml`` (Schema 3, LEG-013) declares these by
dotted path — ``implementation: examples.tools.transform``. A consumer copies
these declarations and swaps the implementation for their own.
"""

from __future__ import annotations


def transform(text: str, factor: int = 2) -> dict:
    """Domain-free tool: plain callable, signature is its contract."""
    return {"transformed": str(text).upper() * factor}


def assess(title: str, summary: str) -> dict:
    """Domain-free tool: consumes a structured ``{title, summary}`` pair."""
    return {"result": f"[{title}] {summary}"}