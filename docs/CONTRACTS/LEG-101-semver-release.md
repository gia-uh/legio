# LEG-101 — semver 0.1 + packaging + changelog + tags

- **Status:** APPROVED by maintainer direction on 2026-09-21 (GitHub #45):
  cut the 0.1.0 release; the changelog, the tag and the release process doc
  ship with the implementation.
- **Closed:** 2026-09-21, GitHub #45 closed as implemented (`7b6774c`; tag
  `v0.1.0` at `73c6ff5`; reproducible from a clean clone).
- **Rasante:** R-10
- **GitHub issue:** #45
- **Source:** `docs/PLAN.md` (LEG-101)
- **Depends on:** all issues merged

## Goal
First release: semantic version `0.1.0`, `uv build` artifacts, changelog
listing every merged issue, and a git tag.

## Scope
- **In scope:** versioning, wheel build, changelog, tag.
- **Out of scope:** publish to a registry (external step, not in-repo).

## Contract & design
- `version = 0.1.0`; `uv build` produces wheel/archive from `src/legio`;
  `CHANGELOG.md` lists every merged issue; `git tag v0.1.0`.
- Release process documented and repeatable.

## Interface
- Distributable wheel + tag `v0.1.0`.

## Acceptance criteria
From `docs/PLAN.md` (LEG-101), verbatim:
- `uv build` produces a wheel/archive; `git tag v0.1.0` exists; changelog lists
  every merged issue.

## Tests
- `uv build` artifact check; tag exists; changelog vs issue list parity.

## Validation case
- Release artifact consumed by LEG-102 consumer.

## Definition of done
- All acceptance criteria met by running checks.
- Maintainer approval recorded; the maintainer closes the GitHub issue.
- Journal entry appended.