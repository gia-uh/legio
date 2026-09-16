"""`legio.config` — runtime node configuration (LEG-081).

The node is configured by two independent files plus the environment:
- ``legio.yaml`` — the general node configuration (node, database, patterns,
  services llm/embedding, api.clients, logging, tools pointer, federation
  known peers), validated through typed pydantic models.
- ``tools.yaml`` — the independent Schema 3 config (LEG-013:
  ``available_tools: {<name>: {implementation, policy}}``), validated with its
  own schema.
- ``LEGIO_*`` environment variables — secrets only, never YAML (LEG-017 §2),
  never logged.

Precedence (highest wins): built-in defaults < config file (explicit argument /
``LEGIO_CONFIG`` env / default ``./legio.yaml``) < CLI overrides. Invalid or
missing values fail loudly with a ``ConfigError`` (unrecoverable, rule 9);
secrets never travel through YAML.
"""

from __future__ import annotations

import logging
import math
import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from legio import naming
from legio.errors import ConfigError, InvalidNameError
from legio.patterns.schema1 import AgentKind

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("legio.yaml")
DEFAULT_TOOLS_PATH = Path("tools.yaml")

SECRET_ENV_NAMES = {
    "llm_api_key": "LEGIO_LLM_API_KEY",
    "embedding_api_key": "LEGIO_EMBEDDING_API_KEY",
    "federation_token": "LEGIO_FEDERATION_TOKEN",
}
CLIENT_TOKEN_PREFIX = "LEGIO_CLIENT_TOKEN_"


def default_node_id() -> str:
    """Built-in default node id ``local@<hostname>`` (host derived at boot)."""
    return f"local@{socket.gethostname()}"


class NodeConfig(BaseModel):
    """Node identity (LEG-016): ``<name>@<host>`` with exactly one ``@``."""

    id: str = Field(default_factory=default_node_id)

    @model_validator(mode="after")
    def _validate_node_id(self) -> NodeConfig:
        naming.validate_node_id(self.id)
        return self


class DatabaseConfig(BaseModel):
    """The beaver database file resource."""

    db_path: Path = Path("legio.db")


class PatternsConfig(BaseModel):
    """One directory per S1 pattern type; recursive ``*.yaml`` scan."""

    tool: Path = Path("./patterns/tool")
    linguistic: Path = Path("./patterns/linguistic")
    composite: Path = Path("./patterns/composite")


class PoolsConfig(BaseModel):
    """Horizontal capacity per pattern (LEG-080) — deployment, not functionality.

    Resolution at class creation (highest wins):
    ``--pool N`` (explicit invocation) > ``per_pattern`` > ``per_kind`` >
    ``default`` > 1. ``per_kind`` admits only the Schema 1 KINDS
    (``tool | linguistic``); ``type: composite`` has no kind (Schema 1) so it
    resolves via ``per_pattern`` or ``default``. A pattern name in
    ``per_pattern`` that is not yet loaded is a visible boot warning — it applies
    if the pattern is created later (dynamic lifecycle). Values are intents; the
    catalog records the real count of instances actually brought up. ``0`` ⇒
    class born disabled.
    """

    per_pattern: dict[str, int] = Field(default_factory=dict)
    per_kind: dict[AgentKind, int] = Field(default_factory=dict)
    default: int | None = None

    @model_validator(mode="after")
    def _validate_pool_counts(self) -> PoolsConfig:
        for level, pools in (
            ("per_pattern", self.per_pattern),
            ("per_kind", self.per_kind),
        ):
            for name, count in pools.items():
                if count < 0:
                    raise ValueError(f"pools.{level}.{name}: pool size must be >= 0")
        if self.default is not None and self.default < 0:
            raise ValueError("pools.default: pool size must be >= 0")
        return self

    def resolve(self, pattern_name: str, *, per_kind: AgentKind | None) -> int | None:
        """Resolve the intended pool size for a pattern (None → the caller's default 1).

        Highest wins: per_pattern > per_kind > default.
        """
        if pattern_name in self.per_pattern:
            return self.per_pattern[pattern_name]
        if per_kind is not None and per_kind in self.per_kind:
            return self.per_kind[per_kind]
        return self.default


_BUILTIN_DRAIN_TIMEOUT = 300.0
_BUILTIN_DRAIN_INTERVAL = 0.05


class LifecycleParams(BaseModel):
    """One level of clock-wait budgets for the runtime's bounded waits (LEG-085).

    ``drain_timeout`` / ``drain_interval`` are the *only* scheduled waits in the
    engine (rule 8 exceptions, §5.8/§10.2): the queue-drain poll of
    ``destroy_class`` and the read-confirm polls of the lifecycle verbs share the
    same budgets (same safety valve). Absent (``None``) fields fall back to the
    built-in or the next lower config level; both must be ``> 0`` when set.
    """

    drain_timeout: float | None = None
    drain_interval: float | None = None

    @model_validator(mode="after")
    def _validate_budgets(self) -> LifecycleParams:
        for name, value in (
            ("drain_timeout", self.drain_timeout),
            ("drain_interval", self.drain_interval),
        ):
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError(f"lifecycle.{name} must be a finite number > 0")
        return self


class LifecycleConfig(BaseModel):
    """Bounded clock-wait budgets per pattern (LEG-085) — the ``lifecycle`` section.

    Resolution per class at verb time (highest wins, partial-field overlay):
    ``per_pattern`` > ``per_kind`` > ``default`` > built-in
    (``drain_timeout`` 300.0 / ``drain_interval`` 0.05). ``per_kind`` admits
    only the Schema 1 KINDS (``tool | linguistic``); ``type: composite`` has no
    kind so it resolves via ``per_pattern`` or ``default``. Each level overlays
    only its non-``None`` fields over the level below, so a level may tune a
    single budget while inheriting the other.
    """

    per_pattern: dict[str, LifecycleParams] = Field(default_factory=dict)
    per_kind: dict[AgentKind, LifecycleParams] = Field(default_factory=dict)
    default: LifecycleParams | None = None

    def resolve(self, pattern_name: str, *, per_kind: AgentKind | None) -> LifecycleParams:
        """Resolve the budgets for a class (built-in < default < per_kind < per_pattern)."""
        params = LifecycleParams(
            drain_timeout=_BUILTIN_DRAIN_TIMEOUT,
            drain_interval=_BUILTIN_DRAIN_INTERVAL,
        )
        if self.default is not None:
            params = _overlay_lifecycle(params, self.default)
        if per_kind is not None and per_kind in self.per_kind:
            params = _overlay_lifecycle(params, self.per_kind[per_kind])
        if pattern_name in self.per_pattern:
            params = _overlay_lifecycle(params, self.per_pattern[pattern_name])
        return params


def _overlay_lifecycle(base: LifecycleParams, layer: LifecycleParams) -> LifecycleParams:
    """Overlay the non-``None`` fields of ``layer`` onto ``base``."""
    return LifecycleParams(
        drain_timeout=layer.drain_timeout if layer.drain_timeout is not None else base.drain_timeout,
        drain_interval=(
            layer.drain_interval if layer.drain_interval is not None else base.drain_interval
        ),
    )


class LlmConfig(BaseModel):
    """`services.llm` — the real `lingo.LLM` constructor args (base_url/model)."""

    base_url: str
    model: str


class EmbeddingConfig(BaseModel):
    """`services.embedding` — sibling service (``lingo.Embedder``).

    ``max_tokens_per_batch`` is optional; absent → lingo's built-in default.
    """

    base_url: str
    model: str = "text-embedding-3-small"
    max_tokens_per_batch: int | None = None


class ServicesConfig(BaseModel):
    """The two sibling inference services. Absent → not configured."""

    llm: LlmConfig | None = None
    embedding: EmbeddingConfig | None = None


class ClientConfig(BaseModel):
    """A registered system in ``api.clients`` (LEG-017 §4).

    ``agents`` is Optional: ``null``/absent → access to all starting agents.
    Token values never live here — they come from env
    (``LEGIO_CLIENT_TOKEN_<NAME>``) per LEG-017 §2.
    """

    agents: list[str] | None = None


class ApiConfig(BaseModel):
    """The HTTP endpoint resource plus the client registry (LEG-017)."""

    host: str = "0.0.0.0"
    port: int = 8000
    clients: dict[str, ClientConfig] = Field(default_factory=dict)


class LoggingConfig(BaseModel):
    """General node log configuration. Absent ``file`` → stream only."""

    level: str = "INFO"
    file: Path | None = None

    @model_validator(mode="after")
    def _validate_level(self) -> LoggingConfig:
        level = self.level.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log level {self.level!r}")
        return self


class ToolsConfig(BaseModel):
    """Pointer to the independent Schema 3 config file."""

    config: Path = DEFAULT_TOOLS_PATH


class PeerConfig(BaseModel):
    """A known peer of THIS node (outbound address book, LEG-017 §5).

    Admission is decided by the shared federation token alone; this list is
    whom the node knows and may delegate to, never an accept allow-list.
    """

    id: str
    url: str

    @model_validator(mode="after")
    def _validate_peer_id(self) -> PeerConfig:
        naming.validate_node_id(self.id)
        return self


class FederationConfig(BaseModel):
    """Known peers of this node. Dormant until federation (R-9) lands."""

    peers: list[PeerConfig] = Field(default_factory=list)


class LegioConfig(BaseModel):
    """The full general node configuration (``legio.yaml``)."""

    node: NodeConfig = Field(default_factory=NodeConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    patterns: PatternsConfig = Field(default_factory=PatternsConfig)
    pools: PoolsConfig = Field(default_factory=PoolsConfig)
    services: ServicesConfig = Field(default_factory=ServicesConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    lifecycle: LifecycleConfig = Field(default_factory=LifecycleConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    federation: FederationConfig = Field(default_factory=FederationConfig)


class ToolPolicy(BaseModel):
    """``policy`` map of a Schema 3 tool declaration (LEG-013)."""

    timeout: int | float | None = None
    retries: int | None = None

    @model_validator(mode="after")
    def _validate_policy(self) -> ToolPolicy:
        if self.timeout is not None and (
            not isinstance(self.timeout, (int, float))
            or isinstance(self.timeout, bool)
            or not math.isfinite(self.timeout)
            or self.timeout <= 0
        ):
            raise ValueError("policy.timeout must be a finite number of seconds > 0")
        if self.retries is not None and (not isinstance(self.retries, int) or self.retries < 0):
            raise ValueError("policy.retries must be an integer >= 0")
        return self


class ToolDeclaration(BaseModel):
    """A Schema 3 tool declaration (LEG-013)."""

    implementation: str
    policy: ToolPolicy = Field(default_factory=ToolPolicy)


class ToolsFileConfig(BaseModel):
    """The independent Schema 3 config file (``tools.yaml``)."""

    available_tools: dict[str, ToolDeclaration] = Field(default_factory=dict)


@dataclass(frozen=True)
class CliOverrides:
    """CLI flag layer: highest-precedence override of built-ins and file."""

    node: str | None = None
    db_path: Path | None = None
    host: str | None = None
    port: int | None = None
    log_level: str | None = None
    tool_dir: Path | None = None
    linguistic_dir: Path | None = None
    composite_dir: Path | None = None
    tools_config: Path | None = None


@dataclass(frozen=True)
class EnvSecrets:
    """Resolved `LEGIO_*` secrets (LEG-017 §2). Never present in YAML."""

    llm_api_key: str | None = None
    embedding_api_key: str | None = None
    federation_token: str | None = None
    client_tokens: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LoadedConfig:
    """The merged node configuration plus its resolved secrets."""

    config: LegioConfig
    secrets: EnvSecrets
    config_path: Path | None = None


def _resolve_config_path(
    config_path: Path | str | None,
    env: Mapping[str, str],
) -> Path | None:
    """Resolve the general config path: argument > ``LEGIO_CONFIG`` > default."""
    if config_path is not None:
        return Path(config_path)
    env_path = env.get("LEGIO_CONFIG")
    if env_path:
        return Path(env_path)
    return DEFAULT_CONFIG_PATH


def _read_yaml(path: Path) -> dict[str, Any]:
    """Read and parse a YAML file into a dict (fail-fast on syntax errors)."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"config file {path} must contain a mapping at the top level")
    return raw


def _merge_overrides(cfg: LegioConfig, overrides: CliOverrides) -> dict[str, Any]:
    """Flatten the current config and apply the CLI overrides."""
    merged = cfg.model_dump()
    if overrides.node is not None:
        merged["node"] = {**merged.get("node", {}), "id": overrides.node}
    if overrides.db_path is not None:
        merged["database"] = {**merged.get("database", {}), "db_path": str(overrides.db_path)}
    if overrides.host is not None:
        merged["api"] = {**merged.get("api", {}), "host": overrides.host}
    if overrides.port is not None:
        merged["api"] = {**merged.get("api", {}), "port": overrides.port}
    if overrides.log_level is not None:
        merged["logging"] = {**merged.get("logging", {}), "level": overrides.log_level}
    patterns = merged.get("patterns", {})
    if overrides.tool_dir is not None:
        patterns["tool"] = str(overrides.tool_dir)
    if overrides.linguistic_dir is not None:
        patterns["linguistic"] = str(overrides.linguistic_dir)
    if overrides.composite_dir is not None:
        patterns["composite"] = str(overrides.composite_dir)
    if patterns:
        merged["patterns"] = patterns
    if overrides.tools_config is not None:
        merged["tools"] = {**merged.get("tools", {}), "config": str(overrides.tools_config)}
    return merged


def _collect_secrets(env: Mapping[str, str]) -> EnvSecrets:
    """Collect the `LEGIO_*` secrets from the environment (LEG-017 §2)."""
    return EnvSecrets(
        llm_api_key=env.get(SECRET_ENV_NAMES["llm_api_key"]),
        embedding_api_key=env.get(SECRET_ENV_NAMES["embedding_api_key"]),
        federation_token=env.get(SECRET_ENV_NAMES["federation_token"]),
        client_tokens={
            key[len(CLIENT_TOKEN_PREFIX) :]: value
            for key, value in env.items()
            if key.startswith(CLIENT_TOKEN_PREFIX)
        },
    )


def load(
    config_path: Path | str | None = None,
    *,
    overrides: CliOverrides | None = None,
    env: Mapping[str, str] | None = None,
) -> LoadedConfig:
    """Load and merge the node configuration (defaults < file < CLI).

    ``config_path`` is the explicit choice (CLI ``--config``); otherwise
    ``LEGIO_CONFIG`` env; otherwise the default ``./legio.yaml``. A missing
    default file yields built-in defaults; a missing/explicitly-given file is
    a ``ConfigError``. Every invalid value surfaces loudly (rule 9).
    """
    environ = env if env is not None else os.environ
    resolved = _resolve_config_path(config_path, environ)

    if resolved is None:
        raw: dict[str, Any] = {}
    elif resolved.is_file():
        logger.info("loading config file path=%s", resolved)
        raw = _read_yaml(resolved)
    else:
        if config_path is not None:
            raise ConfigError(f"config file not found: {resolved}")
        raw = {}

    try:
        cfg = LegioConfig(**raw)
    except (ValidationError, InvalidNameError) as exc:
        logger.error("invalid config file path=%s error=%s", resolved, exc)
        raise ConfigError(f"invalid config {resolved}: {_error_message(exc)}") from exc

    if overrides is not None:
        try:
            cfg = LegioConfig(**_merge_overrides(cfg, overrides))
        except (ValidationError, InvalidNameError) as exc:
            logger.error("invalid CLI overrides error=%s", exc)
            raise ConfigError(f"invalid CLI overrides: {_error_message(exc)}") from exc

    secrets = _collect_secrets(environ)
    path_used = resolved if resolved is not None and resolved.is_file() else None
    return LoadedConfig(config=cfg, secrets=secrets, config_path=path_used)


def load_tools_file(path: Path | str) -> ToolsFileConfig:
    """Load and validate the independent Schema 3 config (LEG-013).

    Enforces, at load time: every tool name passes ``validate_tool_id``, the
    ``implementation`` dotted path is syntactically present, and ``policy``
    follows its shape. Broken declarations fail loudly (rule 9).
    """
    tools_path = Path(path)
    raw = _read_yaml(tools_path)
    try:
        tools = ToolsFileConfig(**raw)
    except (ValidationError, InvalidNameError) as exc:
        logger.error("invalid tools config path=%s error=%s", tools_path, exc)
        raise ConfigError(f"invalid tools config {tools_path}: {_error_message(exc)}") from exc

    for name, decl in tools.available_tools.items():
        try:
            naming.validate_tool_id(name)
            if "." not in decl.implementation:
                raise ConfigError(
                    f"tool {name!r} implementation {decl.implementation!r} is not a dotted path"
                )
            module_part, attr_part = decl.implementation.rsplit(".", 1)
            if not module_part or not attr_part:
                raise ConfigError(
                    f"tool {name!r} implementation {decl.implementation!r} is not a dotted path"
                )
        except InvalidNameError as exc:
            raise ConfigError(f"invalid tools config {tools_path}: {exc}") from exc

    logger.info("tools config loaded path=%s count=%d", tools_path, len(tools.available_tools))
    return tools


def _error_message(exc: ValidationError | InvalidNameError) -> str:
    """A short, human-readable form of a validation failure."""
    if isinstance(exc, InvalidNameError):
        return str(exc)
    first = exc.errors()[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    return f"{location}: {first.get('msg', 'invalid value')}"


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_TOOLS_PATH",
    "ApiConfig",
    "CliOverrides",
    "ClientConfig",
    "ConfigError",
    "DatabaseConfig",
    "EmbeddingConfig",
    "EnvSecrets",
    "FederationConfig",
    "LegioConfig",
    "LifecycleConfig",
    "LifecycleParams",
    "LlmConfig",
    "LoadedConfig",
    "LoggingConfig",
    "NodeConfig",
    "PatternsConfig",
    "PeerConfig",
    "PoolsConfig",
    "ServicesConfig",
    "ToolDeclaration",
    "ToolPolicy",
    "ToolsConfig",
    "ToolsFileConfig",
    "load",
    "load_tools_file",
]
