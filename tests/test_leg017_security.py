"""Red contract tests for LEG-017 — Security contract.

These tests fix the two-level token scheme: one shared federation token for
node-to-node endpoints and per-system client tokens guarded by a single
middleware that enforces the endpoint -> token map. The modules imported here
(``legio.security``, ``legio.security.middleware``) do NOT exist yet. This file
is intentionally red.
"""

from __future__ import annotations

from legio.security import ClientToken, ClientTokenStore, FederationTokenStore
from legio.security.middleware import AuthMiddleware, AuthorizationResult

FEDERATION_ENDPOINTS = [
    "POST /work-items/{agent}",
    "GET /work-items/{id}",
    "GET /outbox",
    "POST /outbox/{id}/ack",
    "GET /catalog",
    "GET /health",
    "POST /deposits",
]

CLIENT_ENDPOINTS = [
    "submit(starting_agent)",
    "status(task_id)",
]


def test_client_token_store_registers_and_checks_token() -> None:
    store = ClientTokenStore()
    store.register("consumer-a", token="t1")
    assert store.is_valid("consumer-a", "t1") is True
    assert store.is_valid("consumer-a", "wrong") is False


def test_client_token_granularity_default_is_all_agents() -> None:
    store = ClientTokenStore()
    token = store.register("consumer-b", token="t2")
    assert isinstance(token, ClientToken)
    assert token.agents is None
    assert store.allowed_starting_agent("consumer-b", "flow_a") is True


def test_client_token_restricted_to_listed_agents() -> None:
    store = ClientTokenStore()
    store.register("consumer-a", token="t1", agents=["flow_a", "flow_b"])
    assert store.allowed_starting_agent("consumer-a", "flow_a") is True
    assert store.allowed_starting_agent("consumer-a", "other") is False


def test_revoking_client_token_blocks_it_immediately() -> None:
    store = ClientTokenStore()
    store.register("consumer-a", token="t1")
    store.revoke("consumer-a")
    assert store.is_valid("consumer-a", "t1") is False
    store.register("consumer-b", token="t2")
    assert store.is_valid("consumer-b", "t2") is True


def test_federation_token_store_shared_secret() -> None:
    store = FederationTokenStore(shared_token="fed-secret")
    assert store.is_valid("fed-secret") is True
    assert store.is_valid("wrong") is False


def test_middleware_endpoint_to_token_map_missing_token_401() -> None:
    middleware = AuthMiddleware(
        federation=("fed-secret", {"peer-a": "http://peer-a"}),
        clients=ClientTokenStore(),
    )
    for endpoint in FEDERATION_ENDPOINTS + CLIENT_ENDPOINTS:
        result = middleware.authorize(endpoint, token=None)
        assert result == AuthorizationResult.UNAUTHORIZED_401


def test_middleware_federation_endpoints_accept_only_federation_token() -> None:
    middleware = AuthMiddleware(
        federation=("fed-secret", {"peer-a": "http://peer-a"}),
        clients=ClientTokenStore(),
    )
    for endpoint in FEDERATION_ENDPOINTS:
        assert middleware.authorize(endpoint, token="fed-secret") in (
            AuthorizationResult.ALLOWED,
            AuthorizationResult.ALLOWED_FEDERATION,
        )
        assert middleware.authorize(endpoint, token="client-token") == (
            AuthorizationResult.UNAUTHORIZED_401
        )


def test_middleware_client_endpoints_accept_only_client_token() -> None:
    clients = ClientTokenStore()
    clients.register("consumer-a", token="t1", agents=["flow_a"])
    middleware = AuthMiddleware(
        federation=("fed-secret", {"peer-a": "http://peer-a"}),
        clients=clients,
    )
    for endpoint in CLIENT_ENDPOINTS:
        assert middleware.authorize(endpoint, token="fed-secret") == (
            AuthorizationResult.UNAUTHORIZED_401
        )
        assert middleware.authorize(endpoint, token="t1") == AuthorizationResult.ALLOWED


def test_middleware_unknown_client_token_is_401() -> None:
    middleware = AuthMiddleware(
        federation=("fed-secret", {"peer-a": "http://peer-a"}),
        clients=ClientTokenStore(),
    )
    assert middleware.authorize("submit(starting_agent)", token="unknown") == (
        AuthorizationResult.UNAUTHORIZED_401
    )


def test_middleware_unknown_peer_is_403() -> None:
    middleware = AuthMiddleware(
        federation=("fed-secret", {"peer-a": "http://peer-a"}),
        clients=ClientTokenStore(),
    )
    assert middleware.authorize("GET /catalog", token="fed-secret", peer_id="evil") == (
        AuthorizationResult.FORBIDDEN_403
    )


def test_middleware_restricted_client_cannot_submit_unlisted_agent() -> None:
    clients = ClientTokenStore()
    clients.register("consumer-a", token="t1", agents=["flow_a"])
    middleware = AuthMiddleware(
        federation=("fed-secret", {"peer-a": "http://peer-a"}),
        clients=clients,
    )
    assert middleware.authorize_agent("submit", token="t1", agent="flow_a") is True
    assert middleware.authorize_agent("submit", token="t1", agent="unlisted") is False


def test_middleware_replaced_does_not_change_endpoint_signatures() -> None:
    """The middleware is pluggable; swapping it keeps authorize(endpoint, token)."""

    class CustomMiddleware(AuthMiddleware):
        pass

    custom = CustomMiddleware(
        federation=("fed-secret", {"peer-a": "http://peer-a"}),
        clients=ClientTokenStore(),
    )
    assert custom.authorize("GET /health", token="fed-secret") in (
        AuthorizationResult.ALLOWED,
        AuthorizationResult.ALLOWED_FEDERATION,
    )
