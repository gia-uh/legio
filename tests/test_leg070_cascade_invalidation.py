"""LEG-070 contract tests: cascade invalidation on invalid dependencies.

The catalog is a dependency DAG: invalidating one (possibly broken) pattern
transitively disables every pattern that depends on it, the catalog reflects
the invalid/disabled set, and the invalidated set is never served. Failures
are never silent (rule 9).
"""

from __future__ import annotations

import pytest

from legio.errors import UnrecoverableError
from legio.patterns import Catalog, load_patterns
from legio.patterns.loader import resolve_branch, resolve_composite_branches


def _linguistic(action: str) -> str:
    """A minimal fictional linguistic agent body (domain-free)."""
    return f"""
name: {action}
type: atomic
kind: linguistic
main: false
input:
  input_as: text
  input_type: json
  input_schema:
    type: object
    properties:
      text: {{type: string}}
output:
  output_as: {action}
  output_type: json
  output_schema:
    type: object
    properties:
      {action}: {{type: string}}
prompt: "{action} the text: {{text}}"
"""


def _composite(name: str, branches: list[list[str]], main: bool = False) -> str:
    """A minimal fictional composite body (domain-free)."""
    branches_yaml = "\n".join(f"    - [{', '.join(branch)}]" for branch in branches)
    return f"""
name: {name}
type: composite
main: {str(main).lower()}
input:
  input_as: payload
  input_type: json
  input_schema:
    type: object
    properties:
      text: {{type: string}}
output:
  output_as: result
  output_type: json
  output_schema:
    type: object
    properties:
      result:
        type: object
        properties:
          result: {{type: string}}
branches:
{branches_yaml}
"""


# Dependency chain: leaf_a -> chain1 -> chain2 -> chain3 (main).
# Multi-branch consumer: fan depends on both leaf_a and leaf_b.
# standalone is referenced by no one.
CHAIN_YAML = f"""
{_linguistic("leaf_a")}
---
{_linguistic("leaf_b")}
---
{_linguistic("standalone")}
---
{_composite("chain1", [["leaf_a"]])}
---
{_composite("chain2", [["chain1"]])}
---
{_composite("chain3", [["chain2"]], main=True)}
---
{_composite("fan", [["leaf_a"], ["leaf_b"]])}
"""


@pytest.fixture()
def chain_catalog() -> Catalog:
    return load_patterns(CHAIN_YAML)


def test_catalog_starts_served_and_consistent(chain_catalog: Catalog) -> None:
    """A freshly loaded catalog serves every pattern; nothing is invalid."""
    catalog = chain_catalog
    for name in ["leaf_a", "leaf_b", "standalone", "chain1", "chain2", "chain3", "fan"]:
        assert name in catalog.specs
        assert catalog.is_served(name)
        assert not catalog.is_invalid(name)
    assert catalog.served() == frozenset(catalog.specs)


def test_chain_cascade_invalidation(chain_catalog: Catalog) -> None:
    """Disabling one broken pattern transitively disables every dependent."""
    catalog = chain_catalog
    newly_invalid = catalog.invalidate("leaf_a")

    assert "leaf_a" in newly_invalid
    assert {"leaf_a", "chain1", "chain2", "chain3", "fan"} <= newly_invalid

    for name in ["leaf_a", "chain1", "chain2", "chain3", "fan"]:
        assert catalog.is_invalid(name)
        assert not catalog.is_served(name)
        assert name not in catalog.served()

    # Independent patterns remain served.
    assert catalog.is_served("leaf_b")
    assert catalog.is_served("standalone")
    assert {"leaf_b", "standalone"} <= catalog.served()


def test_invalidate_unknown_pattern_is_never_silent(chain_catalog: Catalog) -> None:
    """Invalidating a pattern the catalog does not know is a visible error."""
    catalog = chain_catalog
    with pytest.raises(UnrecoverableError, match="unknown pattern"):
        catalog.invalidate("ghost")
    assert catalog.served() == frozenset(catalog.specs)


def test_invalidate_is_idempotent(chain_catalog: Catalog) -> None:
    """Re-invalidating an already invalid pattern is a no-op, not an error."""
    catalog = chain_catalog
    catalog.invalidate("leaf_a")
    first_invalid = catalog.invalid
    assert catalog.invalidate("leaf_a") == frozenset()
    assert catalog.invalid == first_invalid

    catalog.invalidate("leaf_b")
    assert catalog.is_invalid("leaf_b")
    assert catalog.is_invalid("chain1")
    assert catalog.is_invalid("fan")
    assert catalog.is_served("standalone")


def test_branch_referencing_invalid_pattern_is_reported_disabled(
    chain_catalog: Catalog,
) -> None:
    """A branch into an invalid pattern is a reported denial, never masked."""
    catalog = chain_catalog
    spec_chain1 = catalog.specs["chain1"]
    assert catalog.is_served("chain1")
    resolve_composite_branches(spec_chain1, catalog)  # ok while served

    catalog.invalidate("leaf_a")

    with pytest.raises(UnrecoverableError, match="invalid pattern"):
        resolve_branch(["leaf_a"], catalog)
    with pytest.raises(UnrecoverableError, match="invalid pattern"):
        resolve_composite_branches(spec_chain1, catalog)


def test_invalidated_main_agent_not_served_by_starting_route(
    chain_catalog: Catalog,
) -> None:
    """A disabled starting agent is removed from the served catalog (rule 9)."""
    from legio.api import _resolve_route

    catalog = chain_catalog
    assert _resolve_route("chain3", catalog) == (("chain3", "payload"),)

    catalog.invalidate("leaf_a")
    with pytest.raises(UnrecoverableError, match="not served"):
        _resolve_route("chain3", catalog)
