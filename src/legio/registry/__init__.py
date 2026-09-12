"""`legio.registry` — the posterior mirror of the class/instance catalog (LEG-084).

The ``Registry`` (a.k.a. ``AgentRegistry``, AGENT_LIFECYCLE §0/§4.8) is the
**memory of the node's agent state at runtime**: it owns (a) the **live
catalog** — the state of classes / instances / dependencies — and (b) the
**runtime cache of YAML specs** (§4.7). It is a **mirror of facts**:
every entry is written **after** (never before) the corresponding fact occurred,
so the catalog never reports something that does not exist. It never initiates,
never materializes, never runs anything — that is the ``Manager``'s,
orchestrated by the ``Runtime``.

Beaver footprint (Registry-owned scopes, ARCHITECTURE §2): dict ``catalog``
(``name`` → ``ClassRecord``), dict ``instances`` (``class_name:instance_id`` →
``InstanceRecord``), dict ``yaml_cache`` (``name`` → spec text). Registries are
beaver dicts addressed by scope name directly — no invented substrate layer.
"""

from __future__ import annotations

import logging
from enum import Enum

from beaver import AsyncBeaverDB
from pydantic import BaseModel, Field

from legio.patterns.schema1 import AgentKind

logger = logging.getLogger(__name__)

_CATALOG_SCOPE = "catalog"
_INSTANCES_SCOPE = "instances"
_YAML_CACHE_SCOPE = "yaml_cache"


class ActivityState(str, Enum):
    """Activity axis of a class or instance (§3/§4.8): ``enabled``/``disabled``."""

    ENABLED = "enabled"
    DISABLED = "disabled"


class ClassRecord(BaseModel):
    """A class entry in the live catalog (mirror, §4.8).

    ``kind`` is the Schema 1 atomic kind (``AgentKind``); ``None`` represents
    ``type: composite`` (a composite carries no kind). ``state`` is the stored
    explicit decision; reads derive the **effective** state (§4.4).
    """

    name: str
    kind: AgentKind | None
    state: ActivityState = ActivityState.ENABLED
    queue: str
    dependencies: list[str] = Field(default_factory=list)


class InstanceRecord(BaseModel):
    """An instance entry in the live catalog (mirror, §4.8).

    ``instance_id`` is the agent's own identity — never a Manager ``task_id``.
    """

    class_name: str
    instance_id: str
    state: ActivityState = ActivityState.ENABLED


def _instance_key(class_name: str, instance_id: str) -> str:
    return f"{class_name}:{instance_id}"


class Registry:
    """The posterior mirror: records facts, answers granular queries.

    ``Registry`` never initiates, never materializes, never runs — it only
    records what the ``Runtime`` confirms happened and answers read queries
    (AGENT_LIFECYCLE §0/§6). Records persist on the beaver scopes ``catalog``,
    ``instances`` and ``yaml_cache``.

    Idempotence: recording a class/instance that already exists is a no-op;
    removing a non-existent entry is a no-op. ``set_*``/``class_dependencies``
    on a non-existent entry raise (rule 9 — a `set` on nothing would record a
    false fact). ``record_instance`` of a class that is not in the catalog
    raises (no orphan instances). Reads derive the **effective** state (§4.4):
    a stored-``enabled`` class with zero recorded instances reads as
    ``disabled``.
    """

    _CATALOG_SCOPE = _CATALOG_SCOPE
    _INSTANCES_SCOPE = _INSTANCES_SCOPE
    _YAML_CACHE_SCOPE = _YAML_CACHE_SCOPE

    def __init__(self, db: AsyncBeaverDB) -> None:
        if db is None:
            raise TypeError("Registry requires a connected AsyncBeaverDB (beaver system substrate)")
        self._db = db
        self._catalog = db.dict(self._CATALOG_SCOPE)
        self._instances = db.dict(self._INSTANCES_SCOPE)
        self._yaml_cache = db.dict(self._YAML_CACHE_SCOPE)
        logger.info("registry up scopes=catalog,instances,yaml_cache")

    async def record_class(
        self,
        name: str,
        kind: AgentKind | None,
        *,
        dependencies: list[str],
        queue: str,
        state: ActivityState,
    ) -> None:
        """Record the class entry after its queue was created (mirror, §4.8).

        ``kind`` must be an ``AgentKind`` atomic kind or ``None`` (composite).
        Recoding an existing class is a no-op. Logging at INFO decides.
        """
        if kind is not None and not isinstance(kind, AgentKind):
            logger.error("registry record_class deny kind=%r class=%s", kind, name)
            raise ValueError(f"unknown kind {kind!r} for class {name!r}")
        existing = await self._catalog.fetch(name)
        if existing is not None:
            logger.warning("registry record_class noop class=%s (already exists)", name)
            return
        record = ClassRecord(
            name=name,
            kind=kind,
            state=state,
            queue=queue,
            dependencies=list(dependencies),
        )
        await self._catalog.set(name, record.model_dump(mode="json"))
        logger.info(
            "registry record_class class=%s kind=%s state=%s queue=%s deps=%d",
            name,
            kind.value if kind is not None else "composite",
            state.value,
            queue,
            len(dependencies),
        )

    async def cache_spec(self, name: str, yaml: str) -> None:
        """Upsert the class YAML into the cache (§4.7; stored once, after load)."""
        await self._yaml_cache.set(name, yaml)
        logger.info("registry cache_spec class=%s", name)

    async def record_instance(
        self,
        class_name: str,
        instance_id: str,
        *,
        state: ActivityState,
    ) -> None:
        """Record one instance entry after its agent was brought up and is running.

        The class must already be in the catalog (no orphan instances, §5.2).
        Recording an existing instance is a no-op.
        """
        class_data = await self._catalog.fetch(class_name)
        if class_data is None:
            logger.error(
                "registry record_instance deny class=%s instance=%s (class unknown)",
                class_name,
                instance_id,
            )
            raise KeyError(f"unknown class {class_name!r}")
        key = _instance_key(class_name, instance_id)
        existing = await self._instances.fetch(key)
        if existing is not None:
            logger.warning(
                "registry record_instance noop class=%s instance=%s (already exists)",
                class_name,
                instance_id,
            )
            return
        record = InstanceRecord(
            class_name=class_name,
            instance_id=instance_id,
            state=state,
        )
        await self._instances.set(key, record.model_dump(mode="json"))
        logger.info(
            "registry record_instance class=%s instance=%s state=%s",
            class_name,
            instance_id,
            state.value,
        )

    async def set_class_state(self, name: str, state: ActivityState) -> None:
        """Record a class activity change that actually took effect (§4.8)."""
        await self._require_class(name)
        record = ClassRecord.model_validate(await self._catalog.fetch(name))
        record.state = state
        await self._catalog.set(name, record.model_dump(mode="json"))
        logger.info("registry set_class_state class=%s state=%s", name, state.value)

    async def set_instance_state(
        self, class_name: str, instance_id: str, state: ActivityState
    ) -> None:
        """Record an instance activity change that actually took effect (§4.8)."""
        key = _instance_key(class_name, instance_id)
        data = await self._instances.fetch(key)
        if data is None:
            logger.error(
                "registry set_instance_state deny class=%s instance=%s (unknown instance)",
                class_name,
                instance_id,
            )
            raise KeyError(f"unknown instance {instance_id!r} of class {class_name!r}")
        record = InstanceRecord.model_validate(dict(data))
        record.state = state
        await self._instances.set(key, record.model_dump(mode="json"))
        logger.info(
            "registry set_instance_state class=%s instance=%s state=%s",
            class_name,
            instance_id,
            state.value,
        )

    async def remove_instance(self, class_name: str, instance_id: str) -> None:
        """Record an instance whose agent was actually destroyed (§4.8)."""
        key = _instance_key(class_name, instance_id)
        existing = await self._instances.fetch(key)
        if existing is None:
            logger.warning(
                "registry remove_instance noop class=%s instance=%s (absent)",
                class_name,
                instance_id,
            )
            return
        await self._instances.delete(key)
        logger.info(
            "registry remove_instance class=%s instance=%s",
            class_name,
            instance_id,
        )

    async def remove_class(self, name: str) -> None:
        """Remove a class whose spec and queue were actually destroyed (§4.8).

        Armageddon (§4.5): removes the class and **all** its instances; the
        YAML stays cached (§4.7).
        """
        if await self._catalog.fetch(name) is None:
            logger.warning("registry remove_class noop class=%s (absent)", name)
            return
        await self._catalog.delete(name)
        async for key in self._instances.keys():
            if key.startswith(f"{name}:"):
                await self._instances.delete(key)
        logger.info("registry remove_class class=%s", name)

    async def list_classes(self) -> list[ClassRecord]:
        """All classes with their **effective** state (§4.4)."""
        result: list[ClassRecord] = []
        async for data in self._catalog.values():
            record = ClassRecord.model_validate(dict(data))
            record.state = await self._effective_class_state(record.name)
            result.append(record)
        return result

    async def class_state(self, name: str) -> ActivityState | None:
        """The class's **effective** state (existence + activity), or ``None``."""
        if await self._catalog.fetch(name) is None:
            return None
        return await self._effective_class_state(name)

    async def class_dependencies(self, name: str) -> list[str]:
        """The class's direct dependencies (rule 9: unknown class → error)."""
        await self._require_class(name)
        record = ClassRecord.model_validate(await self._catalog.fetch(name))
        return list(record.dependencies)

    async def class_kind(self, name: str) -> AgentKind | None:
        """The class's Schema 1 atomic kind, or ``None`` if unknown/composite.

        A read for resolving per-kind runtime budgets (LEG-085 lifecycle): an
        unknown class and a composite both resolve to ``None`` (composites carry
        no kind, §4.8).
        """
        data = await self._catalog.fetch(name)
        if data is None:
            return None
        return ClassRecord.model_validate(dict(data)).kind

    async def class_dependents(self, name: str, *, transitive: bool = False) -> list[str]:
        """Classes that depend on ``name``; ``transitive`` = the cascade of §7.

        The dependency graph is the inverse of the stored ``dependencies``:
        a class ``X`` is a dependent of ``name`` when ``name`` ∈ ``X.dependencies``.
        Whether directly or transitively, the full upward chain is the set of
        classes that (directly or indirectly) reference ``name``. An unknown
        class has no dependents (empty set).
        """
        if await self._catalog.fetch(name) is None:
            return []
        dependents: set[str] = set()
        async for data in self._catalog.values():
            other = ClassRecord.model_validate(dict(data))
            if name in other.dependencies:
                dependents.add(other.name)
        if transitive:
            frontier: set[str] = set(dependents)
            while frontier:
                next_frontier: set[str] = set()
                for candidate in frontier:
                    async for data in self._catalog.values():
                        other = ClassRecord.model_validate(dict(data))
                        if candidate in other.dependencies and other.name not in dependents:
                            dependents.add(other.name)
                            next_frontier.add(other.name)
                frontier = next_frontier
        return sorted(dependents)

    async def dependencies_satisfied(self, name: str) -> bool:
        """All direct dependencies exist and are effectively enabled (§4.2/§4.3)."""
        dependencies = await self.class_dependencies(name)
        for dependency in dependencies:
            if await self.class_state(dependency) != ActivityState.ENABLED:
                return False
        return True

    async def list_instances(self, class_name: str) -> list[InstanceRecord]:
        """The class's recorded instances and their state (empty if none)."""
        result: list[InstanceRecord] = []
        async for key, data in self._instances.items():
            if key.startswith(f"{class_name}:"):
                result.append(InstanceRecord.model_validate(dict(data)))
        return result

    async def get_instance(self, class_name: str, instance_id: str) -> InstanceRecord | None:
        """One instance record, or ``None`` if it does not exist."""
        key = _instance_key(class_name, instance_id)
        data = await self._instances.fetch(key)
        if data is None:
            return None
        return InstanceRecord.model_validate(dict(data))

    async def get_cached_spec(self, name: str) -> str | None:
        """The cached YAML for ``recreate_class``, or ``None`` (upsert-readable)."""
        data = await self._yaml_cache.fetch(name)
        if data is None:
            return None
        return str(data)

    async def _require_class(self, name: str) -> None:
        if await self._catalog.fetch(name) is None:
            logger.error("registry unknown class=%s", name)
            raise KeyError(f"unknown class {name!r}")

    async def _effective_class_state(self, name: str) -> ActivityState:
        """§4.4: stored-enabled + ≥1 recorded instance ⇒ enabled, else disabled."""
        record = ClassRecord.model_validate(await self._catalog.fetch(name))
        if record.state != ActivityState.ENABLED:
            return ActivityState.DISABLED
        has_instances = False
        async for key in self._instances.keys():
            if key.startswith(f"{name}:"):
                has_instances = True
                break
        return ActivityState.ENABLED if has_instances else ActivityState.DISABLED


__all__ = [
    "ActivityState",
    "AsyncBeaverDB",
    "ClassRecord",
    "InstanceRecord",
    "Registry",
]
