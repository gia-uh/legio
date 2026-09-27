"""LEG-109 — documentation drift guard + peer-roster error (red-first tests).

The drifts the audit found are made machine-checkable here: the example count,
the FlowToken field list, and the stale gate paragraph. The peer-roster error
is asserted to name the peer (rule 9).
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from legio.errors import RecoverableError
from legio.federation import fetch_peer_catalogs
from legio.flow import FlowToken

REPO_ROOT = Path(__file__).resolve().parents[1]
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
ARCHITECTURE = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")


def _flow_token_fields() -> set[str]:
    return set(FlowToken.model_fields)


def test_readme_example_count_matches_the_tree() -> None:
    """The README's example count equals the directories under examples/."""
    directories = sorted(path.name for path in (REPO_ROOT / "examples").iterdir() if path.is_dir())
    count_words = {
        4: "four",
        5: "five",
        6: "six",
    }
    expected_word = count_words[len(directories)]
    match = re.search(r"`examples/` ships (\w+) self-contained", README)
    assert match is not None, "README no longer states the example count"
    assert match.group(1) == expected_word, (
        f"README says {match.group(1)} examples, the tree has {len(directories)}"
    )
    for name in directories:
        assert f"`{name}`" in README or name in README, f"{name} missing from README"


def test_readme_flow_token_field_list_matches_the_model() -> None:
    """The FlowToken field list in the README matches the model exactly."""
    match = re.search(r"Schema 2 `FlowToken` \(([^)]*)\)", README)
    assert match is not None, "README FlowToken field list not found"
    listed = {field.strip().strip("`") for field in match.group(1).split(",")}
    assert listed == _flow_token_fields(), (
        f"README FlowToken fields {sorted(listed)} != model {sorted(_flow_token_fields())}"
    )


def test_architecture_flow_token_fields_match_the_model() -> None:
    """ARCHITECTURE §3's FlowToken field list matches the model (no message_type/payload)."""
    section = ARCHITECTURE.split("Fields (Schema 2):", 1)[1].split("- **Who builds it**", 1)[0]
    listed = {token.strip("`(),") for token in re.findall(r"`[a-z_]+`", section)}
    model_fields = _flow_token_fields()
    # Every model field the section must name.
    missing = model_fields - listed
    assert not missing, f"ARCHITECTURE FlowToken list is missing {sorted(missing)}"
    # The two envelope-only fields must not be claimed as token fields.
    assert "message_type" not in listed and "payload" not in listed


def test_readme_carries_no_stale_gate_debt_paragraph() -> None:
    """The Session-108 format/typecheck debt paragraph is gone from the README."""
    lowered = README.lower()
    assert "false positives" not in lowered
    assert "56 files" not in lowered
    assert "session 108" not in lowered


@pytest.mark.asyncio
async def test_unparseable_peer_roster_names_the_peer() -> None:
    """A malformed roster raises a RecoverableError naming the peer and URL."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"agents": [{"input_as": "x"}]})  # missing 'agent'

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RecoverableError) as excinfo:
            await fetch_peer_catalogs(
                {"peer-b@host-02": "http://host-02:8000"}, "tok", client=client
            )
    message = str(excinfo.value)
    assert "peer-b@host-02" in message
    assert "http://host-02:8000" in message
