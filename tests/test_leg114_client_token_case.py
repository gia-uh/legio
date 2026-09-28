"""LEG-114 — client token env resolves case-insensitively.

The convention is ``LEGIO_CLIENT_TOKEN_<NAME>`` (uppercase), while a client id
may be lowercase. The lookup used to require the exact case, so the documented
env var left the token store empty and every submit answered 401.
"""

from __future__ import annotations

from legio.config import ApiConfig, ClientConfig, EnvSecrets, LegioConfig, LoadedConfig
from legio.materializer import _build_client_store


def _loaded(clients: dict, tokens: dict) -> LoadedConfig:
    return LoadedConfig(
        config=LegioConfig(api=ApiConfig(clients=clients)),
        secrets=EnvSecrets(client_tokens=tokens),
        config_path=None,
    )


def test_uppercase_env_token_matches_lowercase_client() -> None:
    loaded = _loaded({"demo": ClientConfig(agents=["doc_pipeline"])}, {"DEMO": "tok"})
    store = _build_client_store(loaded)
    assert store is not None
    assert store.resolve_consumer_id("tok") == "demo"


def test_lowercase_env_token_still_matches() -> None:
    loaded = _loaded({"client-a": ClientConfig()}, {"client-a": "tok"})
    store = _build_client_store(loaded)
    assert store is not None
    assert store.resolve_consumer_id("tok") == "client-a"


def test_missing_token_is_left_unregistered() -> None:
    loaded = _loaded({"demo": ClientConfig()}, {})
    store = _build_client_store(loaded)
    assert store is not None
    assert store.resolve_consumer_id("tok") is None
