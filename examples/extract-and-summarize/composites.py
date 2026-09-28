"""Concrete composite classes for the ``extract-and-summarize`` example (LEG-110)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from legio.agents import CompositeAgent
from legio.flow import build_payload


class ExtractAndSummarizeComposite(CompositeAgent):
    """Gather the branch payloads and wrap them under this composite's output_as."""

    async def build_output_as(self, info: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        gathered: dict[str, Any] = {}
        for payload in info.values():
            gathered.update(payload)
        return build_payload(gathered, output_as=self._output_as)


COMPOSITE_CLASSES = {"extract_and_summarize": ExtractAndSummarizeComposite}
