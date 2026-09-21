"""Contract tests for LEG-071 — the dry-run validator (GitHub #38).

The spec (docs/CONTRACTS/LEG-071-dry-run-validator.md) is contract-first: the
validator starts from the loader's own choke point
(``_load_specs_from_yaml``), so verdicts are **exactly equivalent** to the boot
load — a tree the dry-run blesses is a tree ``load_pattern_dirs`` (and thus the
boot's refuse-to-serve gate) accepts. The dry-run additionally reports **every**
invalid pattern (not just the first failure) with per-pattern issue lines and a
non-zero CLI exit.

Equivalence invariant under test on both sides:
- clean tree  → no issues  ⇒  ``load_pattern_dirs`` succeeds;
- broken tree → issues (naming every invalid pattern) and
  ``load_pattern_dirs`` raises on the same tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from legio.cli import validate_node
from legio.errors import LegioError
from legio.patterns import load_pattern_dirs
from legio.patterns.loader import ValidationIssue, validate_pattern_dirs

GOOD_ATOMIC = """\
name: done
type: atomic
kind: tool
input:
  input_as: done
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
output:
  output_as: done
  output_type: json
  output_schema:
    type: object
    properties:
      text: {type: string}
tool: done
parameters:
  text: "{done.text}"
"""

GOOD_COMPOSITE = """\
name: woven
type: composite
input:
  input_as: woven
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
output:
  output_as: woven
  output_type: json
  output_schema:
    type: object
    properties:
      text: {type: string}
branches:
  - - done
"""

GHOST_COMPOSITE = """\
name: ghost
type: composite
input:
  input_as: ghost
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
output:
  output_as: ghost
  output_type: json
  output_schema:
    type: object
    properties:
      text: {type: string}
branches:
  - - no-such-step
"""

SHAPE_BAD = """\
name: shape-bad
kind: tool
tool: shape-bad
"""

DUPLICATE = """\
name: dup
type: atomic
kind: tool
input:
  input_as: dup
  input_type: json
  input_schema:
    type: object
    properties:
      text: {type: string}
output:
  output_as: dup
  output_type: json
  output_schema:
    type: object
    properties:
      text: {type: string}
tool: dup
parameters:
  text: "{dup.text}"
"""


def _write_dirs(tmp_path: Path, files: dict[str, str]) -> dict[str, Path]:
    """Write ``patterns/<kind>/<file>`` documents and return the dir map.

    All three per-type dirs must exist for the loader (a configured but missing
    dir is a loud failure), so the empty ones are created — mirroring what a
    node directory ships (and what the LEG-100 walkthrough does).
    """
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    kinds = ("tool", "linguistic", "composite")
    for kind in kinds:
        (tmp_path / "patterns" / kind).mkdir(parents=True, exist_ok=True)
    return {
        "tool": tmp_path / "patterns" / "tool",
        "linguistic": tmp_path / "patterns" / "linguistic",
        "composite": tmp_path / "patterns" / "composite",
    }


def _issues_named(issues: list[ValidationIssue]) -> set[str]:
    return {issue.pattern for issue in issues}


# --- loader-level: the dry-run correctness pass -------------------------------


def test_validate_clean_tree_reports_no_issues_and_equals_boot_load(
    tmp_path: Path,
) -> None:
    """A valid tree: no issues, and the boot's ``load_pattern_dirs`` accepts it."""
    dirs = _write_dirs(
        tmp_path,
        {
            "patterns/tool/done.yaml": GOOD_ATOMIC,
            "patterns/composite/woven.yaml": GOOD_COMPOSITE,
        },
    )
    issues = validate_pattern_dirs(dirs)
    assert issues == []
    catalog = load_pattern_dirs(dirs)
    assert set(catalog.specs) == {"done", "woven"}


def test_validate_reports_every_invalid_pattern_and_matches_boot_failure(
    tmp_path: Path,
) -> None:
    """Three independent breakages (shape, duplicate, unresolvable dep) beside a
    valid pattern: every invalid pattern is reported by name, the valid one is
    not, and the boot load on the same tree fails (equivalence)."""
    dirs = _write_dirs(
        tmp_path,
        {
            "patterns/tool/done.yaml": GOOD_ATOMIC,
            "patterns/tool/shape-bad.yaml": SHAPE_BAD,
            "patterns/tool/dup-a.yaml": DUPLICATE,
            "patterns/tool/dup-b.yaml": DUPLICATE,
            "patterns/composite/ghost.yaml": GHOST_COMPOSITE,
        },
    )
    issues = validate_pattern_dirs(dirs)
    named = _issues_named(issues)
    assert "shape-bad" in named, [issue.detail for issue in issues]
    assert "dup" in named, (  # the second file bearing the name is the conflict
        [issue.detail for issue in issues]
    )
    assert "ghost" in named, [issue.detail for issue in issues]
    assert "done" not in named, [issue.detail for issue in issues]
    assert "no-such-step" in " ".join(issue.detail for issue in issues)

    with pytest.raises(Exception):  # noqa: B017 - the boot gate must refuse
        load_pattern_dirs(dirs)


def test_validate_missing_directory_loud_issue_matching_loader(tmp_path: Path) -> None:
    """A configured but missing patterns dir is a loud issue with the loader's
    own wording (boot refuses such a tree too)."""
    tool = tmp_path / "patterns" / "tool"
    tool.mkdir(parents=True)
    missing = tmp_path / "definitely" / "not" / "here"
    dirs = {"tool": tool, "linguistic": missing, "composite": missing}
    issues = validate_pattern_dirs(dirs)
    assert len(issues) == 2
    for issue in issues:
        assert "pattern directory missing" in issue.detail
        assert issue.file == str(missing)
    with pytest.raises(Exception):  # noqa: B017 - the boot gate must refuse
        load_pattern_dirs(dirs)


def test_validate_unparseable_yaml_is_loud_issue(tmp_path: Path) -> None:
    """A file that does not parse is a loud per-file issue (never silent)."""
    dirs = _write_dirs(
        tmp_path,
        {
            "patterns/tool/broken.yaml": ":\n  - [\n",
            "patterns/tool/done.yaml": GOOD_ATOMIC,
        },
    )
    issues = validate_pattern_dirs(dirs)
    assert len(issues) == 1
    assert "cannot parse" in issues[0].detail
    assert issues[0].file is not None
    assert issues[0].file.endswith("broken.yaml")
    assert _issues_named(issues) == {"?"}


# --- CLI-level: `legio validate --dry-run [--dir DIR]` ------------------------


async def test_validate_cli_clean_node_exits_zero(tmp_path: Path, capsys) -> None:
    """A clean node tree validates with exit code 0 and an ok line."""
    _write_dirs(
        tmp_path,
        {
            "patterns/tool/done.yaml": GOOD_ATOMIC,
            "patterns/composite/woven.yaml": GOOD_COMPOSITE,
        },
    )
    result = await validate_node(None, directory=tmp_path)
    assert result == 0
    assert "all patterns valid" in capsys.readouterr().out


async def test_validate_cli_broken_node_exits_nonzero_and_reports(tmp_path: Path, capsys) -> None:
    """A broken node tree exits non-zero with a per-pattern error line each."""
    _write_dirs(
        tmp_path,
        {
            "patterns/tool/done.yaml": GOOD_ATOMIC,
            "patterns/tool/shape-bad.yaml": SHAPE_BAD,
            "patterns/composite/ghost.yaml": GHOST_COMPOSITE,
        },
    )
    result = await validate_node(None, directory=tmp_path)
    assert result == 1
    captured = capsys.readouterr()
    assert "validate error" in captured.err
    assert "shape-bad" in captured.err
    assert "ghost" in captured.err
    assert "done" not in captured.err


async def test_validate_cli_requires_dir_or_config() -> None:
    """Neither ``--dir`` nor ``--config`` is a loud caller error (rule 9)."""
    with pytest.raises(LegioError, match="--dir|--config"):
        await validate_node(None, directory=None)
