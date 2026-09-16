"""Contract tests for LEG-091 — step resolver (required agent → local | remote).

The author resolves each step before deposit: local when the node serves the
agent (``pattern_catalog.served()``, the same capacity LEG-090 exposes),
remote when a configured peer's catalog offers it with a matching
``schema_version``, error otherwise. The resolver is a *pure decision*: no
beaver, no HTTP, no deposits — it only decides, and it raises before any
queuing can happen (pre-deposit gate, ARCH §9).

Local-first framing (ARCH §9 + maintainer, session 85q): federation transports
work, never lifecycle. The resolver therefore never decides to control another
node's agents — only whether a *step* executes at home or on a peer.
"""

from __future__ import annotations

import logging

import pytest

from legio.federation import (
    AgentInterface,
    InterfaceMismatchError,
    Local,
    Remote,
    StepResolver,
    UnresolvableAgentError,
)
from legio.flow import SCHEMA_VERSION
from legio.patterns import Catalog, load_patterns

logger = logging.getLogger("legio.tests.leg091")

NODE_ALPHA = "alpha@hub"
NODE_BETA = "beta@hub"

TOOL_YAML = """\
name: cutter
type: atomic
kind: tool
input:
  input_as: payload
  input_type: json
  input_schema:
    type: object
    properties:
      raw: {type: string}
output:
  output_as: result
  output_type: json
  output_schema:
    type: object
    properties:
      result: {type: string}
tool: cutter
parameters:
  raw: "{payload.raw}"
"""

LING_YAML = """\
name: admirer
type: atomic
kind: linguistic
input:
  input_as: payload
  input_type: json
  input_schema:
    type: object
    properties:
      raw: {type: string}
output:
  output_as: result
  output_type: json
  output_schema:
    type: object
    properties:
      result: {type: string}
prompt: "Admire {raw}."
"""


def capacity_catalog() -> Catalog:
    """The node's served capacity: cutter (tool), admirer (linguistic)."""
    return load_patterns(TOOL_YAML + "---\n" + LING_YAML)


def resolver(**peer_catalogs) -> StepResolver:
    """A resolver over the served capacity plus the given peer rosters.

    Keyword arguments are ``peer_id=roster`` where a roster maps agent name →
    interface (the shape of a LEG-090 ``GET /catalog`` response's agents).
    """
    return StepResolver(
        local_capacity=capacity_catalog().served(),
        peer_catalogs=peer_catalogs,
    )


def interface(capability: str, *, schema_version: int = SCHEMA_VERSION) -> AgentInterface:
    return AgentInterface(capability=capability, schema_version=schema_version)


# --------------------------------------------------------------------------
# § local resolution
# --------------------------------------------------------------------------


def test_resolves_served_agent_to_local() -> None:
    result = resolver().resolve("cutter")
    assert result == Local(agent="cutter")
    assert not isinstance(result, Remote)


def test_local_capacity_tracks_served_patterns() -> None:
    catalog = capacity_catalog()
    catalog.invalidate("cutter")
    r = StepResolver(local_capacity=catalog.served(), peer_catalogs={})
    with pytest.raises(UnresolvableAgentError):
        r.resolve("cutter")


# --------------------------------------------------------------------------
# § remote resolution
# --------------------------------------------------------------------------


def test_resolves_peer_offered_agent_to_remote() -> None:
    r = resolver(**{NODE_BETA: {"polisher": interface("polisher")}})
    result = r.resolve("polisher")
    assert result == Remote(
        peer_id=NODE_BETA,
        agent="polisher",
        interface=interface("polisher"),
    )


def test_offered_agent_with_mismatched_schema_version_fails_loudly() -> None:
    r = resolver(**{NODE_BETA: {"polisher": interface("polisher", schema_version=999)}})
    with pytest.raises(InterfaceMismatchError):
        r.resolve("polisher")


def test_mismatched_peer_is_not_silently_skipped_for_a_later_match() -> None:
    r = resolver(
        **{
            NODE_BETA: {"polisher": interface("polisher", schema_version=999)},
            "gamma@hub": {"polisher": interface("polisher")},
        }
    )
    with pytest.raises(InterfaceMismatchError):
        r.resolve("polisher")


def test_peer_without_the_agent_is_skipped_and_later_peer_decides() -> None:
    r = resolver(
        **{
            NODE_BETA: {"other": interface("other")},
            "gamma@hub": {"polisher": interface("polisher")},
        }
    )
    result = r.resolve("polisher")
    assert result == Remote(
        peer_id="gamma@hub",
        agent="polisher",
        interface=interface("polisher"),
    )


def test_first_offering_peer_in_order_wins() -> None:
    r = resolver(
        **{
            NODE_BETA: {"polisher": interface("polisher")},
            "gamma@hub": {"polisher": interface("polisher")},
        }
    )
    result = r.resolve("polisher")
    assert isinstance(result, Remote)
    assert result.peer_id == NODE_BETA


# --------------------------------------------------------------------------
# § error path
# --------------------------------------------------------------------------


def test_unresolvable_agent_raises() -> None:
    with pytest.raises(UnresolvableAgentError):
        resolver().resolve("ghost")


# --------------------------------------------------------------------------
# § purity (the pre-deposit gate)
# --------------------------------------------------------------------------


def test_resolve_is_pure_never_deposits() -> None:
    r = resolver(**{NODE_BETA: {"polisher": interface("polisher")}})
    before_served = capacity_catalog().served()
    assert r.resolve("admirer") == Local(agent="admirer")
    assert r.resolve("polisher") == Remote(
        peer_id=NODE_BETA, agent="polisher", interface=interface("polisher")
    )
    assert r.resolve("cutter") == Local(agent="cutter")
    assert capacity_catalog().served() == before_served