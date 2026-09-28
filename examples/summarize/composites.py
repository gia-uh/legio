"""Concrete composite classes for the ``summarize`` example node (LEG-110).

The engine has no generic composite build (LEG-040): the composite's output
construction is the pattern's model. A node declares its concrete classes here
and ``boot_node`` loads them through the node config's ``composites.config``
pointer, so ``legio server --config <node>/legio.yaml`` boots the node.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from legio.agents import CompositeAgent
from legio.flow import build_payload


class SummarizeComposite(CompositeAgent):
    """Gather the branch payloads and wrap them under this composite's output_as."""

    async def build_output_as(self, info: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        gathered: dict[str, Any] = {}
        for payload in info.values():
            gathered.update(payload)
        return build_payload(gathered, output_as=self._output_as)


COMPOSITE_CLASSES = {"summarize": SummarizeComposite}
