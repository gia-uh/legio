"""`legio.security.middleware` — the authorization decision helper (LEG-017).

Encodes the endpoint → token map: federation (node-to-node) endpoints accept
only the shared federation token (and a known peer), while client endpoints
accept only a registered client token. Enforcement itself is inline in the
served surface (`api.py`); this module is the pure, tested decision unit and
the pluggable hook a consumer app may wrap or replace — any subtype keeps
the same ``authorize(endpoint, token)`` signature.
"""

from __future__ import annotations

import logging
from enum import Enum

from legio.security import ClientTokenStore

logger = logging.getLogger(__name__)

#: Federation paths (ARCH §10): node-to-node endpoints only. A `METHOD /path`
#: string is federation only for this explicit set — never for any method
#: prefix (a client surface such as `POST /submit` must not read as
#: federation). Templated routes (`/work-items/{agent}`, `/outbox/{id}/ack`)
#: match by prefix; anything else falls through to the client branch.
_FEDERATION_EXACT_PATHS = frozenset({"/catalog", "/deposits", "/health", "/outbox"})
_FEDERATION_PATH_PREFIXES = ("/work-items/", "/outbox/")


class AuthorizationResult(str, Enum):
    """The outcome of an authorization decision."""

    ALLOWED = "allowed"
    ALLOWED_FEDERATION = "allowed_federation"
    UNAUTHORIZED_401 = "unauthorized_401"
    FORBIDDEN_403 = "forbidden_403"


class AuthMiddleware:
    """Enforces client and federation token rules per endpoint."""

    def __init__(
        self,
        *,
        federation: tuple[str, dict[str, str]],
        clients: ClientTokenStore,
    ) -> None:
        self._shared_token, self._peers = federation
        self._clients = clients

    def _is_federation_endpoint(self, endpoint: str) -> bool:
        _, _, path = endpoint.partition(" ")
        if not path.startswith("/"):
            return False
        return path in _FEDERATION_EXACT_PATHS or path.startswith(_FEDERATION_PATH_PREFIXES)

    def _token_to_consumer_id(self, token: str) -> str | None:
        return self._clients.resolve_consumer_id(token)

    def authorize(
        self,
        endpoint: str,
        token: str | None,
        peer_id: str | None = None,
    ) -> AuthorizationResult:
        """Authorize an endpoint against a token, optionally a peer origin."""
        result = self._decide(endpoint, token, peer_id)
        if result not in (AuthorizationResult.ALLOWED, AuthorizationResult.ALLOWED_FEDERATION):
            logger.warning(
                "auth denied endpoint=%s token=%r peer=%s result=%s",
                endpoint,
                bool(token),
                peer_id,
                result.value,
            )
        return result

    def _decide(
        self,
        endpoint: str,
        token: str | None,
        peer_id: str | None,
    ) -> AuthorizationResult:
        """Compute the authorization outcome without side effects."""
        if not token:
            return AuthorizationResult.UNAUTHORIZED_401

        if self._is_federation_endpoint(endpoint):
            if token != self._shared_token:
                return AuthorizationResult.UNAUTHORIZED_401
            if peer_id is not None and peer_id not in self._peers:
                return AuthorizationResult.FORBIDDEN_403
            return (
                AuthorizationResult.ALLOWED_FEDERATION
                if peer_id is not None
                else AuthorizationResult.ALLOWED
            )

        if token == self._shared_token:
            return AuthorizationResult.UNAUTHORIZED_401
        if self._token_to_consumer_id(token) is None:
            return AuthorizationResult.UNAUTHORIZED_401
        return AuthorizationResult.ALLOWED

    def authorize_agent(self, action: str, token: str, agent: str) -> bool:
        """Whether the client token may submit to ``agent``."""
        consumer_id = self._token_to_consumer_id(token)
        if consumer_id is None:
            return False
        return self._clients.allowed_starting_agent(consumer_id, agent)


__all__ = ["AuthMiddleware", "AuthorizationResult"]
