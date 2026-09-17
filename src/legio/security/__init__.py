"""`legio.security` — the two-level token scheme (LEG-017).

One shared federation token guards node-to-node endpoints; per-system client
tokens guard client submit/status endpoints. Enforcement is inline in the
served surface (`api.py`); `AuthMiddleware` (`legio.security.middleware`) is
the pure decision helper encoding the same endpoint → token map, kept as the
pluggable hook a consumer app may wrap or replace.
"""

from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


def _secrets_equal(left: str, right: str) -> bool:
    """Constant-time secret comparison (no early-exit timing signal)."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


@dataclass
class ClientToken:
    """A per-system client token.

    ``agents`` optionally restricts which starting agents the token may submit;
    ``None`` (default) means all agents.
    """

    consumer_id: str
    token: str
    agents: list[str] | None = None


class ClientTokenStore:
    """Registers, checks, restricts and revokes client tokens."""

    def __init__(self) -> None:
        self._tokens: dict[str, ClientToken] = {}

    def register(
        self,
        consumer_id: str,
        *,
        token: str,
        agents: list[str] | None = None,
    ) -> ClientToken:
        """Register (or re-register) a consumer's token."""
        for registered in self._tokens.values():
            if registered.consumer_id != consumer_id and _secrets_equal(registered.token, token):
                raise ValueError(
                    f"token already held by consumer {registered.consumer_id!r}: "
                    "one secret, one owner (ambiguous ownership is refused)"
                )
        registered = ClientToken(consumer_id=consumer_id, token=token, agents=agents)
        self._tokens[consumer_id] = registered
        # The secret itself is never logged (rule 11 observes the decision,
        # never the credential).
        logger.info(
            "client token registered consumer=%s restricted=%s",
            consumer_id,
            agents is not None,
        )
        return registered

    def revoke(self, consumer_id: str) -> None:
        """Immediately invalidate a consumer's token."""
        if self._tokens.pop(consumer_id, None) is None:
            logger.info("client token revoke noop consumer=%s (unknown)", consumer_id)
            return
        logger.info("client token revoked consumer=%s", consumer_id)

    def is_valid(self, consumer_id: str, token: str) -> bool:
        stored = self._tokens.get(consumer_id)
        return stored is not None and _secrets_equal(stored.token, token)

    def resolve_consumer_id(self, token: str) -> str | None:
        """Return the consumer id holding ``token``, or ``None`` if unknown."""
        for registered in self._tokens.values():
            if _secrets_equal(registered.token, token):
                return registered.consumer_id
        return None

    def allowed_starting_agent(self, consumer_id: str, agent: str) -> bool:
        stored = self._tokens.get(consumer_id)
        if stored is None:
            return False
        if stored.agents is None:
            return True
        return agent in stored.agents


class FederationTokenStore:
    """Validates a single shared federation secret (standalone store)."""

    def __init__(self, shared_token: str) -> None:
        self._shared_token = shared_token

    def is_valid(self, token: str) -> bool:
        return _secrets_equal(token, self._shared_token)


__all__ = ["ClientToken", "ClientTokenStore", "FederationTokenStore"]
