"""`legio.cli` — the Runtime CLI (LEG-081, R-8).

Two typer commands on the installed ``legio`` console script:

- ``legio server --config path`` — node bootstrap: loads the LEG-017 config,
  boots the node (materializer), brings the served catalog up (§8), and serves
  ``submit``/``status`` over HTTP (plus the federation surface when a
  ``LEGIO_FEDERATION_TOKEN`` is configured). ``SIGTERM``/``SIGINT`` stop the
  server: the host stops driving the executor between dispatches (LEG-083/085
  one-pass ``manager.run()``), an in-flight step completes, then the process
  exits — no engine seam (rule 8).
- ``legio agent <command>`` — the lifecycle verbs (§4.8 of AGENT_LIFECYCLE),
  reachable through ``Runtime.deposit_node_op`` for the enable/disable/destroy
  verbs (LEG-088 accept) and through the direct Runtime verbs for
  create/recreate/instances/reads. Control signing keys are per-boot,
  in-process, never persisted (LEG-082), so each ``legio agent`` invocation
  boots the node in-process (a fresh key + matching verifiers) and drives its
  own executor — there is no lifecycle-over-HTTP surface (federation
  transports work, never lifecycle).

The CLI is a host, never the engine: it owns the executor pumps, it never
sleeps (the pump loop awaits each dispatch with ``asyncio.sleep(0)`` between
passes), and the bounded clock-waits it performs (operator confirms, server
shutdown settle) are the host-side equivalents of the engine's own §5.8
budgets. Domain-free (rule 7): every command speaks the abstract lifecycle
vocabulary, never consumer-domain names.

Imported ``services.llm``/``legio` lingo factory and ``composite_classes``
remain resource seams (rule 7): they are never declared by this module — the
materializer defaults apply (a linguistic agent without an LLM service or a
composite without a concrete class refuses the boot loudly, naming the agent).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import threading
from collections.abc import Callable, Coroutine
from pathlib import Path
from time import monotonic
from typing import Annotated, Any

import typer
import uvicorn
import yaml

from legio import logging as legio_logging
from legio.config import CliOverrides, LoadedConfig, load
from legio.errors import ConfigError, LegioError
from legio.manager import TaskStatus
from legio.materializer import BootedNode, boot_node
from legio.patterns.loader import load_patterns, split_yaml_documents
from legio.runtime import Runtime

logger = logging.getLogger(__name__)

#: Executor pool doctrine (§6.1): every live instance occupies one executor for
#: its whole life (the parked bring-up generator), and the control/report/result
#: drains + seeds need spares. The fleet is sized from the served pools plus a
#: fixed spare margin.
_SPARE_EXECUTORS = 3
_MIN_EXECUTORS = 3
#: How long the server host waits for the non-vehicle executor fleet to settle
#: at shutdown before cancelling the standing-agent pumps (host-side settle,
#: not an engine timer — rule 8's exception applies to the engine, the host
#: owns this bounded wait).
_SHUTDOWN_SETTLE = 0.5

app = typer.Typer(
    name="legio",
    help="Runtime CLI: boot a node (`legio server`) or drive the lifecycle "
    "verbs (`legio agent`). Domain-free: YAML patterns + a tools registry are "
    "the only data.",
    no_args_is_help=True,
)

agent_cmd = typer.Typer(
    help="Runtime lifecycle verbs over the node booted in-process (LEG-088 "
    "intake for enable/disable/destroy; direct Runtime verbs on reads).",
    no_args_is_help=True,
    invoke_without_command=True,
)
app.add_typer(agent_cmd, name="agent")


# --- config resolution --------------------------------------------------------


def _apply_overrides(
    node: str | None,
    db_path: Path | None,
    log_level: str | None,
    tool_dir: Path | None,
    linguistic_dir: Path | None,
    composite_dir: Path | None,
    tools: Path | None,
) -> CliOverrides:
    """Assemble the CLI override layer (LEG-081 config precedence, session 58)."""
    return CliOverrides(
        node=node,
        db_path=db_path,
        log_level=log_level,
        tool_dir=tool_dir,
        linguistic_dir=linguistic_dir,
        composite_dir=composite_dir,
        tools_config=tools,
    )


def _configure_logging(loaded: LoadedConfig) -> None:
    """Consumer-owned logging config (rule 11): legio never configures the root
    logger on its own; the CLI is the consumer, so it opts in via the helper."""
    level = loaded.config.logging.level
    legio_logging.configure(level=level)
    file = loaded.config.logging.file
    if file is not None:
        handler = logging.FileHandler(file, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s")
        )
        handler.setLevel(level)
        logging.getLogger("legio").addHandler(handler)


# --- executor ownership (the host drives the pumps, never the Runtime) --------


def executor_pump_count(booted: BootedNode) -> int:
    """Size the executor fleet from the served pools + spares (§6.1 doctrine)."""
    total = 0
    for name, spec in booted.catalog.specs.items():
        if not booted.catalog.is_served(name):
            continue
        pool = booted.config.config.pools.resolve(name, per_kind=spec.kind)
        total += pool if pool is not None else 1
    return max(_MIN_EXECUTORS, total + _SPARE_EXECUTORS)


async def executor_loop(runtime: Runtime, should_stop: Callable[[], bool]) -> None:
    """The node's executor: one-pass `manager.run()` per dispatch (LEG-085),
    parked at ``asyncio.sleep(0)`` between passes. The host chooses when to
    stop driving — between dispatches, so an in-flight step completes first."""
    while not should_stop():
        await runtime.manager.run()
        await asyncio.sleep(0)


async def _shutdown_pumps(
    pumps: list[asyncio.Task], stop: asyncio.Event, grace: float = _SHUTDOWN_SETTLE
) -> None:
    """Host-side shutdown: stop scheduling, let in-flight steps settle, then
    cancel whatever still runs (the standing-agent pumps) so the event loop
    closes clean. No engine seam: pumps stop between dispatches on their own;
    the cancel lands only after ``grace`` seconds, when the in-flight step had
    its chance to complete."""
    stop.set()
    deadline = monotonic() + grace
    alive = set(pumps)
    while alive:
        if monotonic() >= deadline:
            break
        _, alive = await asyncio.wait(alive, timeout=grace)
    settled = len(alive)
    for task in alive:
        task.cancel()
    if settled:
        logger.info("cli executor shutdown cancelled_pumps=%d (standing agents)", settled)
    if alive:
        await asyncio.gather(*alive, return_exceptions=True)


# --- §8 catalog bootstrap ------------------------------------------------------


def _collect_spec_yamls(loaded: LoadedConfig) -> dict[str, str]:
    """Collect ``name -> YAML text`` for every pattern document in the three
    dirs, to feed the runtime YAML cache (§8 step 3, §4.7 recreate-class)."""
    yamls: dict[str, str] = {}
    directories = [
        loaded.config.patterns.tool,
        loaded.config.patterns.linguistic,
        loaded.config.patterns.composite,
    ]
    for directory in directories:
        for yaml_file in sorted(Path(directory).rglob("*.yaml")):
            try:
                source = yaml_file.read_text(encoding="utf-8")
                segments = split_yaml_documents(source)
                values = [yaml.safe_load(segment) for segment in segments]
            except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
                raise ConfigError(f"cannot parse pattern file {yaml_file}: {exc}") from exc
            for segment, value in zip(segments, values, strict=True):
                for name in _document_names(value):
                    yamls[name] = segment
    return yamls


def _document_names(value: object) -> list[str]:
    """The ``name`` fields a parsed YAML document declares (dict or list of dicts)."""
    if isinstance(value, dict):
        name = value.get("name")
        return [str(name)] if isinstance(name, str) else []
    if isinstance(value, list):
        names: list[str] = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                names.append(str(item["name"]))
        return names
    return []


async def bring_catalog_up(booted: BootedNode) -> None:
    """The §8 bootstrap: bring the served catalog up in topological order.

    Caller (the server host) must have the executor pumps running first — a
    real bring-up needs dispatchers alive for its confirmations (session 86e).
    """
    await booted.runtime.create_from_catalog(
        booted.catalog,
        pools=booted.config.config.pools,
        spec_yamls=_collect_spec_yamls(booted.config),
    )
    logger.info(
        "cli catalog up classes=%d",
        len(await booted.runtime.list_classes()),
    )


# --- operator confirm (host-side bounded clock wait, §5.8-family) --------------


async def _operator_budget(booted: BootedNode, class_name: str) -> tuple[float, float]:
    kind = await booted.runtime.class_kind(class_name)
    params = booted.config.config.lifecycle.resolve(class_name, per_kind=kind)
    return (
        params.drain_timeout if params.drain_timeout is not None else 300.0,
        params.drain_interval if params.drain_interval is not None else 0.05,
    )


async def _await_operator(
    runtime: Runtime, task_id: str, *, timeout: float, interval: float, what: str
) -> None:
    """Wait a deposited node-op drain to a terminal state (the pump fleet drives
    it). A bounded host-side wait mirroring the engine's §5.8 budgets."""
    deadline = monotonic() + timeout
    while True:
        record = await runtime.manager.status(task_id)
        if record is not None and record.status in (TaskStatus.SUCCESS, TaskStatus.FAILED):
            if record.status is TaskStatus.FAILED:
                raise LegioError(f"{what} failed: {record.error or 'unknown reason'}")
            return
        if monotonic() >= deadline:
            last = await runtime.manager.status(task_id)
            state = last.status.value if last is not None else "unknown"
            raise LegioError(
                f"{what} did not reach terminal within {timeout}s (state={state})"
            )
        await asyncio.sleep(interval)


# --- the agent verbs -----------------------------------------------------------


_LIFECYCLE_VERBS = {
    "enable-class": "enable_class",
    "disable-class": "disable_class",
    "destroy-class": "destroy_class",
    "enable-instance": "enable_instance",
    "disable-instance": "disable_instance",
    "destroy-instance": "destroy_instance",
}
_STATE_LABEL = "absent"


def _as_int(value: object, default: int) -> int:
    """Coerce a CLI option value to int (typer hands validated ints; the async
    seam passes them as ``object``). ``None`` → the default."""
    if value is None:
        return default
    return int(str(value))


async def agent_command(
    loaded: LoadedConfig, command: str, **options: object
) -> list[str]:
    """Run one lifecycle verb against the node booted in-process.

    The invocation boots the node (a per-boot control key + matching
    verifiers), starts the executor fleet, dispatches the verb, and tears its
    process-bound engine down — the durable truth (classes, YAML cache)
    persists on the shared substrate; live instances are process-bound.
    """
    booted = await boot_node(loaded)
    count = executor_pump_count(booted)
    stop = asyncio.Event()
    pumps = [
        asyncio.create_task(executor_loop(booted.runtime, stop.is_set))
        for _ in range(count)
    ]
    try:
        return await _dispatch_command(booted, command, options)
    finally:
        await _shutdown_pumps(pumps, stop)
        if booted.agents_db is not None:
            await booted.agents_db.aclose()
        await booted.db.close()


async def _dispatch_command(
    booted: BootedNode, command: str, options: dict[str, object]
) -> list[str]:
    """Map one verb name to the Runtime surface (1:1, thin — no new logic)."""
    runtime = booted.runtime
    name = str(options.get("name") or "")
    instance_id = str(options["instance_id"]) if options.get("instance_id") else None

    if command == "create-class":
        spec_path = Path(str(options["spec"]))
        yaml_text = spec_path.read_text(encoding="utf-8")
        catalog = load_patterns(yaml_text)
        specs = list(catalog.specs.values())
        if not specs:
            raise LegioError(f"spec file {spec_path} declares no pattern")
        if len(specs) > 1:
            logger.warning(
                "create-class multiple specs file=%s count=%d using_first=%s",
                spec_path,
                len(specs),
                specs[0].name,
            )
        pool = _as_int(options.get("pool"), 1)
        await runtime.create_class(specs[0], spec_yaml=yaml_text, pool=pool)
        return [f"class created name={specs[0].name} pool={pool}"]

    if command == "recreate-class":
        if not name:
            raise LegioError("recreate-class requires a class name")
        pool = _as_int(options.get("pool"), 1)
        await runtime.recreate_class(name, pool=pool)
        return [f"class recreated name={name} pool={pool}"]

    if command == "create-instance":
        if not name:
            raise LegioError("create-instance requires a class name")
        count = _as_int(options.get("count"), 1)
        created = await runtime.create_instance(name, count=count)
        return [f"instance created class={name} id={instance_id}" for instance_id in created]

    if command in _LIFECYCLE_VERBS:
        if not name:
            raise LegioError(f"{command} requires a class name")
        verb = _LIFECYCLE_VERBS[command]
        if verb.endswith("_instance") and not instance_id:
            raise LegioError(f"{command} requires an instance id")
        timeout, interval = await _operator_budget(booted, name)
        task_id = await runtime.deposit_node_op(verb, name, instance_id)
        await _await_operator(
            runtime,
            task_id,
            timeout=timeout,
            interval=interval,
            what=f"{command} {name}{' ' + instance_id if instance_id else ''}",
        )
        message = f"{command.replace('-', ' ')} {name}"
        if instance_id:
            message = f"{message} {instance_id}"
        if command == "destroy-class":
            message = f"{message} mode={options.get('mode') or 'drain'}"
        return [f"{message} ok"]

    if command == "list-classes":
        lines: list[str] = []
        for record in await runtime.list_classes():
            kind = record.kind.value if record.kind is not None else "composite"
            lines.append(f"{record.name}\t{kind}\t{record.state.value}")
        return lines

    if command == "class-deps":
        if not name:
            raise LegioError("class-deps requires a class name")
        deps = await runtime.class_dependencies(name)
        return deps or [_STATE_LABEL]

    if command == "class-dependents":
        if not name:
            raise LegioError("class-dependents requires a class name")
        dependents = await runtime.class_dependents(name, transitive=True)
        return dependents or [_STATE_LABEL]

    if command == "class-state":
        if not name:
            raise LegioError("class-state requires a class name")
        state = await runtime.class_state(name)
        return [state.value if state is not None else _STATE_LABEL]

    if command == "list-instances":
        if not name:
            raise LegioError("list-instances requires a class name")
        records = await runtime.list_instances(name)
        return [f"{record.instance_id}\t{record.state.value}" for record in records]

    raise LegioError(f"unknown agent command {command!r}")


# --- typer command layer -------------------------------------------------------


def _run_cli(coro: Coroutine[Any, Any, int]) -> None:
    """Run an async CLI command, converting failures into a loud CLI exit."""
    try:
        result = asyncio.run(coro)
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None
    except LegioError as exc:
        typer.echo(f"legio error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if result != 0:
        raise typer.Exit(code=result)


@app.command("server")
def server_command(
    config: Annotated[
        Path | None, typer.Option("--config", help="Node config file (LEG-017).")
    ] = None,
    node: Annotated[
        str | None, typer.Option("--node", help="Override node id (<name>@<host>).")
    ] = None,
    db_path: Annotated[
        Path | None, typer.Option("--db-path", help="Override the database path.")
    ] = None,
    host: Annotated[
        str | None, typer.Option("--host", help="Override the HTTP bind host.")
    ] = None,
    port: Annotated[
        int | None, typer.Option("--port", help="Override the HTTP bind port.")
    ] = None,
    log_level: Annotated[
        str | None, typer.Option("--log-level", help="Override the log level.")
    ] = None,
    tool_dir: Annotated[
        Path | None, typer.Option("--tool-dir", help="Override the tool patterns dir.")
    ] = None,
    linguistic_dir: Annotated[
        Path | None, typer.Option("--linguistic-dir", help="Override the linguistic patterns dir.")
    ] = None,
    composite_dir: Annotated[
        Path | None, typer.Option("--composite-dir", help="Override the composite patterns dir.")
    ] = None,
    tools: Annotated[
        Path | None, typer.Option("--tools", help="Override the Schema 3 tools file.")
    ] = None,
    federation: Annotated[
        bool,
        typer.Option(
            "--federation",
            help="Serve the federation surface (requires LEGIO_FEDERATION_TOKEN;"
            " the surface also mounts automatically when the token is set).",
        ),
    ] = False,
) -> None:
    """Boot the node and serve submit/status. SIGTERM/SIGINT drain in-flight
    work before exit (the host stops driving the executor between dispatches)."""
    loaded = load(
        config,
        overrides=_apply_overrides(
            node, db_path, log_level, tool_dir, linguistic_dir, composite_dir, tools
        ),
    )
    _configure_logging(loaded)
    _run_cli(serve_node(loaded, host=host, port=port, federation=federation))


@agent_cmd.callback()
def agent_options(
    ctx: typer.Context,
    config: Annotated[
        Path | None, typer.Option("--config", help="Node config file (LEG-017).")
    ] = None,
    node: Annotated[
        str | None, typer.Option("--node", help="Override node id (<name>@<host>).")
    ] = None,
    db_path: Annotated[
        Path | None, typer.Option("--db-path", help="Override the database path.")
    ] = None,
    log_level: Annotated[
        str | None, typer.Option("--log-level", help="Override the log level.")
    ] = None,
    tool_dir: Annotated[
        Path | None, typer.Option("--tool-dir", help="Override the tool patterns dir.")
    ] = None,
    linguistic_dir: Annotated[
        Path | None, typer.Option("--linguistic-dir", help="Override the linguistic patterns dir.")
    ] = None,
    composite_dir: Annotated[
        Path | None, typer.Option("--composite-dir", help="Override the composite patterns dir.")
    ] = None,
    tools: Annotated[
        Path | None, typer.Option("--tools", help="Override the Schema 3 tools file.")
    ] = None,
) -> None:
    """Shared node options (`legio agent <verb>`)."""
    ctx.obj = _apply_overrides(
        node, db_path, log_level, tool_dir, linguistic_dir, composite_dir, tools
    )
    ctx.meta["legio_config"] = config


def _agent_run(ctx: typer.Context, command: str, **options: object) -> None:
    loaded = load(ctx.meta.get("legio_config"), overrides=ctx.obj)
    _configure_logging(loaded)

    def _dispatch() -> None:
        lines = asyncio.run(agent_command(loaded, command, **options))
        for line in lines:
            typer.echo(line)

    try:
        _dispatch()
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None
    except LegioError as exc:
        typer.echo(f"legio error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@agent_cmd.command("create-class")
def agent_create_class(
    ctx: typer.Context,
    spec: Annotated[Path, typer.Argument(help="The pattern spec file (YAML).")],
    pool: Annotated[int, typer.Option("--pool", min=0, help="Pool intent (LEG-080).")] = 1,
) -> None:
    """Create a class from its pattern spec file."""
    _agent_run(ctx, "create-class", spec=spec, pool=pool)


@agent_cmd.command("recreate-class")
def agent_recreate_class(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The class name.")],
    pool: Annotated[int, typer.Option("--pool", min=0, help="Pool intent (LEG-080).")] = 1,
) -> None:
    """Recreate a class from its cached YAML (§4.7)."""
    _agent_run(ctx, "recreate-class", name=name, pool=pool)


@agent_cmd.command("enable-class")
def agent_enable_class(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The class name.")],
) -> None:
    """Enable a class through the node-op intake (LEG-088)."""
    _agent_run(ctx, "enable-class", name=name)


@agent_cmd.command("disable-class")
def agent_disable_class(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The class name.")],
) -> None:
    """Disable a class through the node-op intake (LEG-088)."""
    _agent_run(ctx, "disable-class", name=name)


@agent_cmd.command("destroy-class")
def agent_destroy_class(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The class name.")],
    mode: Annotated[
        str,
        typer.Option(
            "--mode",
            help="drain: wait the queue to drain; now: skip the class queue drain.",
        ),
    ] = "drain",
) -> None:
    """Destroy a class (drain or now) through the node-op intake (LEG-088)."""
    _agent_run(ctx, "destroy-class", name=name, mode=mode)


@agent_cmd.command("create-instance")
def agent_create_instance(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The class name.")],
    count: Annotated[int, typer.Option("--count", min=1, help="How many instances.")] = 1,
) -> None:
    """Create one or more instances of an existing class."""
    _agent_run(ctx, "create-instance", name=name, count=count)


@agent_cmd.command("enable-instance")
def agent_enable_instance(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The class name.")],
    instance_id: Annotated[str, typer.Argument(help="The instance id.")],
) -> None:
    """Enable one instance through the node-op intake (LEG-088)."""
    _agent_run(ctx, "enable-instance", name=name, instance_id=instance_id)


@agent_cmd.command("disable-instance")
def agent_disable_instance(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The class name.")],
    instance_id: Annotated[str, typer.Argument(help="The instance id.")],
) -> None:
    """Disable one instance through the node-op intake (LEG-088)."""
    _agent_run(ctx, "disable-instance", name=name, instance_id=instance_id)


@agent_cmd.command("destroy-instance")
def agent_destroy_instance(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The class name.")],
    instance_id: Annotated[str, typer.Argument(help="The instance id.")],
) -> None:
    """Destroy one instance through the node-op intake (LEG-088)."""
    _agent_run(ctx, "destroy-instance", name=name, instance_id=instance_id)


@agent_cmd.command("list-classes")
def agent_list_classes(ctx: typer.Context) -> None:
    """List the live classes (registry mirror)."""
    _agent_run(ctx, "list-classes")


@agent_cmd.command("class-deps")
def agent_class_deps(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The class name.")],
) -> None:
    """List a class's declared dependencies."""
    _agent_run(ctx, "class-deps", name=name)


@agent_cmd.command("class-dependents")
def agent_class_dependents(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The class name.")],
) -> None:
    """List the (transitive) dependents of a class."""
    _agent_run(ctx, "class-dependents", name=name)


@agent_cmd.command("class-state")
def agent_class_state(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The class name.")],
) -> None:
    """Show a class's effective activity state."""
    _agent_run(ctx, "class-state", name=name)


@agent_cmd.command("list-instances")
def agent_list_instances(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The class name.")],
) -> None:
    """List the instances of a class."""
    _agent_run(ctx, "list-instances", name=name)


# --- the server ----------------------------------------------------------------


def _install_exit_signal_handler() -> Callable[[], None]:
    """Install a host-level SIGINT/SIGTERM handler that just records the intent.

    Uvicorn's ``Server.capture_signals`` installs its own ``handle_exit`` (which
    sets ``should_exit``) and, on exit, **restores the original handler and
    re-raises the captured signal**. Without a host-level handler, that re-raise
    lands on the default disposition and kills the process before ``serve_node``
    can close its own resources. With it, uvicorn restores *ours* and the
    re-raised signal is absorbed — the host finishes its shutdown (executor
    settle, subscriber close) and exits 0. Signals only bind the main thread.
    """
    recorded = False

    def _record(signum: int, frame: object) -> None:
        nonlocal recorded
        recorded = True

    prior: dict[int, object] = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            prior[sig] = signal.signal(sig, _record)

    def _restore() -> None:
        if threading.current_thread() is threading.main_thread():
            for sig, previous in prior.items():
                signal.signal(sig, previous)  # type: ignore[arg-type]

    return _restore


async def serve_node(
    loaded: LoadedConfig,
    *,
    host: str | None = None,
    port: int | None = None,
    federation: bool = False,
) -> int:
    """Boot the node, bring the catalog up, and serve until SIGTERM/SIGINT.

    Configuration wins over option arguments: an explicit CLI ``--host``/
    ``--port`` override the config's ``api.host``/``api.port``.
    """
    if federation and loaded.secrets.federation_token is None:
        raise LegioError(
            "--federation requires LEGIO_FEDERATION_TOKEN (the federation "
            "surface is guarded by the shared token, LEG-017 §2)"
        )
    booted = await boot_node(loaded)
    bind_host = host or loaded.config.api.host
    bind_port = port or loaded.config.api.port
    count = executor_pump_count(booted)
    restore_signals = _install_exit_signal_handler()
    server = uvicorn.Server(
        uvicorn.Config(booted.app, host=bind_host, port=bind_port, log_level="warning")
    )
    pumps = [
        asyncio.create_task(executor_loop(booted.runtime, lambda: server.should_exit))
        for _ in range(count)
    ]
    stop = asyncio.Event()
    try:
        await bring_catalog_up(booted)
        logger.info(
            "cli server ready node=%s host=%s port=%s federation=%s",
            loaded.config.node.id,
            bind_host,
            bind_port,
            loaded.secrets.federation_token is not None,
        )
        await server.serve()
        logger.info("cli server stopped node=%s", loaded.config.node.id)
    finally:
        restore_signals()
        await _shutdown_pumps(pumps, stop, grace=_SHUTDOWN_SETTLE)
        if booted.agents_db is not None:
            await booted.agents_db.aclose()
        await booted.db.close()
    return 0


def main() -> None:
    """Console entry point (``legio`` and ``python -m legio``)."""
    app()


__all__ = [
    "agent_command",
    "app",
    "bring_catalog_up",
    "executor_loop",
    "executor_pump_count",
    "main",
    "serve_node",
]