# LEG-071 — Dry-run validator + strict fail-fast startup

- **Status:** APPROVED by maintainer direction on 2026-09-21 (GitHub #38):
  implement the validate command; the startup gate (refuse to serve an invalid
  catalog) already ships with the boot's loud loader refusal.
- **Closed:** 2026-09-21, GitHub #38 closed as implemented (`7ca42b8`; red-first in
  `tests/test_leg071_validate.py`; `make ci` green, 640 passed).
- **Rasante:** R-7
- **GitHub issue:** #38
- **Source:** `docs/PLAN.md` (LEG-071)
- **Depends on:** LEG-021

## Goal
Operational safety: `legio validate --dry-run` reports every invalid pattern
and exits non-zero; startup refuses to serve any catalog containing an invalid
pattern.

## Scope
- **In scope:** the validate CLI command, startup gate.
- **Out of scope:** package CLI bootstrapping details beyond validate
  (LEG-081).

## Contract & design
- `validate --dry-run` = load + correctness pass (deps exist, schema-valid,
  no cycle) with per-pattern and exit-code reporting.
- Startup: if any pattern invalid → refuse to serve (before listening),
  motivated by LEG-070 invisibility (never serve silently-broken).

## Interface
- `legio validate --dry-run [--dir DIR]` → exit != 0 on any invalid.

## Acceptance criteria
From `docs/PLAN.md` (LEG-071), verbatim:
- `legio validate --dry-run` reports every invalid pattern and exits non-zero;
  startup refuses to serve a catalog with any invalid pattern.

## Tests
- CLI contract tests (red first): invalid fixture → non-zero, startup refusal.
  Ship: `tests/test_leg071_validate.py` (loader-level `validate_pattern_dirs`
  equivalence with `load_pattern_dirs` on both clean and broken trees; CLI-level
  `legio validate --dry-run` exit codes and per-pattern stderr lines).

## Validation case
- Injecting a broken pattern fixture (end-to-end: `python -m legio validate
  --dry-run --dir` on a broken node tree → `validate error` lines + exit 1;
  on `examples/transform` → `validate ok` + exit 0).

## Definition of done
- All acceptance criteria met by running checks.
- Maintainer approval recorded; the maintainer closes the GitHub issue.
- Journal entry appended.