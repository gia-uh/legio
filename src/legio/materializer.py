"""`legio.materializer` — the engine piece that boots a node from config (LEG-081).

The Runtime is the missing engine piece: from a `LoadedConfig` it connects the
database, loads **and validates** the three pattern directories first (any
invalid pattern refuses the boot before anything binds — rule 9), loads the
independent Schema 3 tools file into the tool registry, and *materializes* the
standing agent map from the validated Catalog through the injected resource
seams (tool registry, lingo client factory, concrete composite classes).

- **Tool atoms** are built from `spec.tool` + `spec.parameters` against the
  Schema 3 registry. A reference to an undeclared tool is a visible boot error
  naming the agent and the tool (rule 9).
- **Linguistic atoms** are built with the compiled `output_model` (LEG-072,
  from the pattern's `output_schema` — never a guessed shape) and a single
  shared lingo client produced by the injected factory. The default factory
  builds `lingo.LLM(model, base_url, api_key)` from `services.llm`; tests and
  consumers substitute `MockLLM`. A linguistic spec with no `services.llm` and
  no injected factory is a visible boot error naming the agent.
- **Composites** are built from their resolved branches (LEG-040/044) through
  the injected concrete classes (the composite's `build_output_as` is the
  pattern's model — the engine never guesses it). A composite without a
  provided class is a visible boot error naming the composite.
- Agents are built in DAG order (atomics first, composites after), reporting
  each through the optional ``on_built`` progress hook.

`boot_node` then builds the HTTP app and the authenticated client store
(LEG-017) from `api.clients` + `LEGIO_CLIENT_TOKEN_*` secrets; a configured
client without its token is warned and left unregistered (visible, rule 9).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from beaver import AsyncBeaverDB
from fastapi import FastAPI

from legio.agents import AgentBase, CompositeAgent, LinguisticAgent, ToolAgent
from legio.api import create_app
from legio.config import LlmConfig, LoadedConfig
from legio.errors import UnrecoverableError
from legio.flow import ControlVerifier, derive_control_key
from legio.patterns import (
    AgentKind,
    AgentSpec,
    AgentType,
    Catalog,
    load_pattern_dirs,
    resolve_composite_branches,
)
from legio.patterns.compile import compile_schema
from legio.runtime import Runtime
from legio.security import ClientTokenStore
from legio.tools import AvailableToolsRegistry

logger = logging.getLogger(__name__)

LingoFactory = Callable[[LlmConfig | None, str | None], Any]
CompositeClasses = Mapping[str, type[CompositeAgent]]


def default_lingo_factory(llm: LlmConfig | None, api_key: str | None) -> Any:
    """Default seam: build ``lingo.LLM(model, base_url, api_key)`` (LEG-081).

    ``services.llm`` unset → a visible failure (rule 9): no LLM was configured
    for a node whose catalog asks for one.
    """
    if llm is None:
        logger.error("no services.llm for the lingo factory")
        raise UnrecoverableError(
            "no services.llm configured and no lingo_factory injected"
        )
    from lingo.llm import LLM

    return LLM(model=llm.model, base_url=llm.base_url, api_key=api_key)


def _materialize_atom(
    spec: AgentSpec,
    *,
    db: AsyncBeaverDB,
    available_tools: AvailableToolsRegistry,
    lingo_client: Any,
    control_verifier: ControlVerifier | None,
) -> AgentBase:
    """Materialize a single atomic agent (tool or linguistic) or raise."""
    if spec.kind is AgentKind.TOOL:
        if spec.tool not in available_tools.all_declarations():
            logger.error("tool not declared agent=%s tool=%s", spec.name, spec.tool)
            raise UnrecoverableError(
                f"tool agent {spec.name!r} references undeclared tool {spec.tool!r}"
            )
        return ToolAgent(
            agent_id=spec.name,
            db=db,
            available_tools=available_tools,
            tool_name=spec.tool or "",
            parameters=spec.parameters or {},
            input_as=spec.input.input_as,
            output_as=spec.output.output_as,
            input_schema=spec.input.input_schema,
            output_schema=spec.output.output_schema,
            control_verifier=control_verifier,
        )

    if spec.kind is AgentKind.LINGUISTIC:
        if spec.output.output_schema is None:
            raise UnrecoverableError(
                f"linguistic agent {spec.name!r} has no output_schema to compile"
            )
        output_model = compile_schema(spec.output.output_schema)
        return LinguisticAgent(
            agent_id=spec.name,
            db=db,
            lingo_client=lingo_client,
            prompt_template=spec.prompt or "",
            output_model=output_model,
            input_as=spec.input.input_as,
            output_as=spec.output.output_as,
            input_schema=spec.input.input_schema,
            output_schema=spec.output.output_schema,
            control_verifier=control_verifier,
        )

    raise UnrecoverableError(f"atomic agent {spec.name!r} has unknown kind: {spec.kind}")


def _materialize_composite(
    spec: AgentSpec,
    *,
    catalog: Catalog,
    db: AsyncBeaverDB,
    composite_classes: CompositeClasses,
    control_verifier: ControlVerifier | None,
) -> AgentBase:
    """Materialize one composite through its concrete class, or raise."""
    composite_type = composite_classes.get(spec.name)
    if composite_type is None:
        logger.error("no concrete composite class agent=%s", spec.name)
        raise UnrecoverableError(
            f"composite {spec.name!r} has no concrete composite class injected; "
            "the composite's build_output_as is the pattern's model"
        )
    branches = resolve_composite_branches(spec, catalog)
    return composite_type(
        agent_id=spec.name,
        db=db,
        branches=branches,
        input_as=spec.input.input_as,
        output_as=spec.output.output_as,
        input_schema=spec.input.input_schema,
        output_schema=spec.output.output_schema,
        control_verifier=control_verifier,
    )


def materialize_agents(
    catalog: Catalog,
    *,
    db: AsyncBeaverDB,
    available_tools: AvailableToolsRegistry,
    llm_config: LlmConfig | None = None,
    llm_api_key: str | None = None,
    lingo_factory: LingoFactory | None = None,
    composite_classes: CompositeClasses | None = None,
    control_verifiers: Mapping[str, ControlVerifier] | None = None,
    on_built: Callable[[str], None] | None = None,
) -> dict[str, AgentBase]:
    """Build the standing agent map from a validated catalog, in DAG order.

    Atomics are materialized first, composites second (a composite's branches
    reference already-materialized atomics or other composites by name —
    LEG-070 DAG order). Every spec is validated at load, so an unmaterializable
    spec here is a *boot-time* failure — visible and naming the agent (rule 9).
    ``control_verifiers`` (name → verify-only handle, LEG-082/LEG-087) is the
    boot's injection seam: absent, the agents hold no verifier.
    """
    classes = composite_classes or {}
    lingo_client: Any | None = None
    agents: dict[str, AgentBase] = {}
    verifiers = control_verifiers or {}

    def _lingo() -> Any:
        nonlocal lingo_client
        if lingo_client is None:
            factory = lingo_factory or default_lingo_factory
            lingo_client = factory(llm_config, llm_api_key)
        return lingo_client

    def _served_specs() -> list[AgentSpec]:
        return [spec for spec in catalog.specs.values() if catalog.is_served(spec.name)]

    # DAG order: every atomic is materialized before any composite (a composite
    # references the atomics by name — LEG-070 DAG over composite branches).
    for spec in _served_specs():
        if spec.type is not AgentType.ATOMIC:
            continue
        if spec.kind is AgentKind.LINGUISTIC:
            try:
                agent = _materialize_atom(
                    spec,
                    db=db,
                    available_tools=available_tools,
                    lingo_client=_lingo(),
                    control_verifier=verifiers.get(spec.name),
                )
            except UnrecoverableError as exc:
                raise UnrecoverableError(
                    f"linguistic agent {spec.name!r}: {exc.message}"
                ) from exc
        else:
            agent = _materialize_atom(
                spec,
                db=db,
                available_tools=available_tools,
                lingo_client=None,
                control_verifier=verifiers.get(spec.name),
            )
        agents[spec.name] = agent
        if on_built is not None:
            on_built(spec.name)
        logger.info("agent materialized agent=%s type=%s kind=%s", spec.name, spec.type, spec.kind)

    for spec in _served_specs():
        if spec.type is not AgentType.COMPOSITE:
            continue
        agent = _materialize_composite(
            spec,
            catalog=catalog,
            db=db,
            composite_classes=classes,
            control_verifier=verifiers.get(spec.name),
        )
        agents[spec.name] = agent
        if on_built is not None:
            on_built(spec.name)
        logger.info("agent materialized agent=%s type=%s kind=%s", spec.name, spec.type, spec.kind)

    return agents


def _build_client_store(
    loaded: LoadedConfig,
) -> ClientTokenStore | None:
    """Register the configured clients with their env tokens (LEG-017)."""
    clients = loaded.config.api.clients
    if not clients:
        return None
    store = ClientTokenStore()
    for consumer_id, client_cfg in clients.items():
        token = loaded.secrets.client_tokens.get(consumer_id)
        if token is None:
            logger.warning(
                "client has no token env=LEGIO_CLIENT_TOKEN_%s consumer=%s",
                consumer_id.upper(),
                consumer_id,
            )
            continue
        store.register(consumer_id, token=token, agents=client_cfg.agents)
    return store


def _build_tool_registry(tools: LoadedConfig) -> AvailableToolsRegistry:
    """Declare every Schema 3 tool of the node's tools config file."""
    from legio.config import load_tools_file

    tools_config = load_tools_file(tools.config.tools.config)
    registry = AvailableToolsRegistry()
    for name, declaration in tools_config.available_tools.items():
        registry.declare(
            name,
            implementation=declaration.implementation,
            policy=(
                declaration.policy.model_dump()
                if declaration.policy is not None
                else None
            ),
        )
    return registry


def available_tools_from_config(loaded: LoadedConfig) -> AvailableToolsRegistry:
    """Public alias: the node's Schema 3 tool registry from its tools config."""
    return _build_tool_registry(loaded)


async def boot_node(
    loaded: LoadedConfig,
    *,
    db: AsyncBeaverDB | None = None,
    lingo_factory: LingoFactory | None = None,
    composite_classes: CompositeClasses | None = None,
    control_key: bytes | None = None,
    on_built: Callable[[str], None] | None = None,
) -> BootedNode:
    """Boot a node from its LoadedConfig: connect → load → validate → materialize.

    Ordered, fail-fast (rule 9): the database connects, the three pattern dirs
    load *and validate* (any invalid pattern refuses the boot before any agent
    binds), the Schema 3 tools register, and only then the standing agents
    materialize. ``db`` lets tests inject their database (the node's shared
    substrate); otherwise the configured path is opened directly. ``control_key``
    is the per-boot, in-process key the lifecycle facts mint with (LEG-082); a
    boot without one derives it from an entropy draw (random per boot, rule 11).
    The Runtime (LEG-085), the HTTP app and the authenticated client store are
    built last.
    """
    cfg = loaded.config

    if db is None:
        database = AsyncBeaverDB(str(cfg.database.db_path))
        await database.connect()
        owns_database = True
    else:
        database = db
        owns_database = False

    try:
        return await _boot_on_database(
            loaded,
            database,
            lingo_factory=lingo_factory,
            composite_classes=composite_classes,
            control_key=control_key,
            on_built=on_built,
        )
    except BaseException:
        if owns_database:
            logger.warning("node boot failed; closing connected db path=%s", cfg.database.db_path)
            await database.close()
        raise


async def _boot_on_database(
    loaded: LoadedConfig,
    database: AsyncBeaverDB,
    *,
    lingo_factory: LingoFactory | None,
    composite_classes: CompositeClasses | None,
    control_key: bytes | None,
    on_built: Callable[[str], None] | None,
) -> BootedNode:
    """Boot the node over an already-connected substrate (fail-fast, rule 9)."""
    cfg = loaded.config
    key = control_key if control_key is not None else derive_control_key(os.urandom(32))
    engine = Runtime(database, node_id=cfg.node.id, control_key=key)
    pattern_dirs = {
        "tool": cfg.patterns.tool,
        "linguistic": cfg.patterns.linguistic,
        "composite": cfg.patterns.composite,
    }
    catalog = load_pattern_dirs(pattern_dirs)

    registry = available_tools_from_config(loaded)
    served = {name for name in catalog.specs if catalog.is_served(name)}
    verifiers = {name: ControlVerifier(key) for name in served}
    agents = materialize_agents(
        catalog,
        db=database,
        available_tools=registry,
        llm_config=cfg.services.llm,
        llm_api_key=loaded.secrets.llm_api_key,
        lingo_factory=lingo_factory,
        composite_classes=composite_classes,
        control_verifiers=verifiers,
        on_built=on_built,
    )
    engine.mount_agents(agents)

    client_store = _build_client_store(loaded)
    booted = BootedNode(
        config=loaded,
        agents=agents,
        catalog=catalog,
        db=database,
        runtime=engine,
        client_store=client_store,
    )
    logger.info(
        "node booted db=%s node=%s agents=%d starting=%s",
        database,
        cfg.node.id,
        len(agents),
        ",".join(sorted(booted.starting_agents)),
    )
    return booted


@dataclass(frozen=True)
class BootedNode:
    """A booted node: config, connected db, catalog, standing agents, runtime and app."""

    config: LoadedConfig
    agents: Mapping[str, AgentBase]
    catalog: Catalog
    db: AsyncBeaverDB
    runtime: Runtime
    client_store: ClientTokenStore | None = None

    @property
    def app(self) -> FastAPI:
        """The HTTP app exposing the Runtime's submit/status over REST (LEG-025).

        When a federation token is configured (LEG-017 L1 secret), the app also
        serves the federation surface guarded by that shared token: ``GET
        /catalog`` (LEG-090), ``POST /work-items/{agent}`` (LEG-092) and the
        outbox verbs ``GET``/``DELETE /outbox/{task_id}`` (LEG-093).
        """
        return create_app(
            runtime=self.runtime,
            clients=self.client_store,
            pattern_catalog=self.catalog,
            federation_token=self.config.secrets.federation_token,
        )

    @property
    def starting_agents(self) -> frozenset[str]:
        """The served ``main`` patterns — the node's starting agents."""
        return frozenset(spec.name for spec in self.catalog.specs.values() if spec.main)


__all__ = [
    "BootedNode",
    "available_tools_from_config",
    "boot_node",
    "default_lingo_factory",
    "materialize_agents",
]