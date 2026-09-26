"""Node-local tool implementation for the ``transform`` example.

This file lives beside ``legio.yaml`` and is declared by dotted path
``tools.transform``: the node directory is the import root for node-local
tools (LEG-104), so the example stays self-contained wherever it is copied.
"""

from __future__ import annotations


def transform(text: str, factor: int = 2) -> dict:
    """Domain-free tool: plain callable, its signature is its contract."""
    return {"transformed": str(text).upper() * factor}
