"""`legio.agents.linguistic_agent` — the linguistic agent runner (LEG-030, over LEG-023).

Runs a linguistic step of a route: resolves its prompt template against the
payload (LEG-010 H2 dotted paths + system vars), asks an injected lingo client
(an ``LLM``/``MockLLM`` fake) for a structured pydantic record validated
against the pattern's compiled ``output_schema``, and builds the new payload
via the re-implementable seam ``build_output_as`` (whose basic linguistic model
wraps the record's dump under ``output_as``; a pattern may inherit
``LinguisticAgent`` and re-implement it). The base routes by position
(finality by position + ``level``, Schema 2). The step's state travels in the
messages (AGENT_LIFECYCLE §12.1): nothing is staged out-of-message.

The call is a single ``create(model, [system prompt])`` round-trip (LEG-030 v1
call contract). Failures from lingo are never silent (AGENTS.md rule 9): a
raised step error is routed by the base to an error result.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from lingo.llm import Message
from pydantic import BaseModel

from legio.agents.base import AgentBase
from legio.flow import ControlVerifier, ExecutionRequestMessage, build_payload
from legio.patterns.template import resolve_template

logger = logging.getLogger(__name__)


class LinguisticAgent(AgentBase):
    """Runs a single linguistic step against a lingo client and the payload."""

    def __init__(
        self,
        *,
        agent_id: str,
        db: Any,
        lingo_client: Any,
        prompt_template: str,
        output_model: type[BaseModel],
        system_vars: Mapping[str, Any] | None = None,
        input_as: str = "",
        output_as: str = "",
        input_schema: Mapping[str, Any] | None = None,
        output_schema: Mapping[str, Any] | None = None,
        control_verifier: ControlVerifier | None = None,
    ) -> None:
        super().__init__(
            agent_id=agent_id,
            db=db,
            output_as=output_as,
            input_schema=input_schema,
            output_schema=output_schema,
            control_verifier=control_verifier,
        )
        self._lingo = lingo_client
        self._prompt = prompt_template
        self._output_model = output_model
        self._input_as = input_as
        ready_vars = dict(system_vars or {})
        ready_vars.setdefault("current_date", datetime.now(UTC).date().isoformat())
        self._system_vars = ready_vars

    async def _handle(self, request: ExecutionRequestMessage) -> dict[str, Any]:
        scoped: dict[str, Any] = (
            dict(request.payload.get(self._input_as, {}))
            if self._input_as and self._input_as in request.payload
            else dict(request.payload)
        )
        logger.debug(
            "linguistic input agent=%s task=%s keys=%s",
            self._agent_id,
            request.task_id,
            ",".join(scoped),
        )
        prompt = resolve_template(self._prompt, scoped, self._system_vars)
        logger.info(
            "linguistic call agent=%s task=%s",
            self._agent_id,
            request.task_id,
        )
        messages = [Message.system(prompt)]
        result = await self._lingo.create(self._output_model, messages)
        logger.info(
            "linguistic result agent=%s task=%s",
            self._agent_id,
            request.task_id,
        )
        return await self.build_output_as(result.model_dump())

    async def build_output_as(self, info: Any) -> dict[str, Any]:
        """Basic linguistic-output model: the record's dump is the value.

        ``info`` is the structured record's ``model_dump()`` — already validated
        against the pattern's compiled ``output_schema`` by lingo. Override this
        seam (inherit ``LinguisticAgent``) to shape the value otherwise before it
        is wrapped under ``output_as`` (§12.1).
        """
        return build_payload(info, output_as=self._output_as)


__all__ = ["LinguisticAgent"]
