# LEG-107 — Release integrity and dependency hygiene (`v0.1.1`)

- **Status:** APPROVED by maintainer direction on 2026-09-26 (GitHub #56).
- **Rasante:** R-10.x (hardening of the released `v0.1.0`)
- **GitHub issue:** #56 (findings 4/7/8 of the #52 audit umbrella)
- **Source:** external audit of `v0.1.0` (`fbd787e`), findings 4, 7, 8
- **Depends on:** LEG-101 (semver/packaging), LEG-102 (artifact validation)

---

## Goal

The published release is internally consistent: the tag certifies the tree it
sits on, the changelog lists everything inside the tag, the validation record
postdates the certified tree, and consumer-installed dependencies are bounded.

## Problem

- **Finding 4.** The **published** tag `v0.1.0` is on `fbd787e`, three commits
  past the release commit `7b6774c`. `CHANGELOG.md [0.1.0]` never lists
  LEG-063/LEG-064 (inside the tag); the validation record
  (`2026-09-21T22:18:47Z`) predates the resilience commit (`23:11Z`) and the
  tagged tree by ~1,200 lines. (The dev clone's local tag is stale at
  `73c6ff5`; the published ref is `fbd787e`.)
- **Finding 7.** `beaver-db` and `lingo-ai` have no version bounds in
  `pyproject.toml`; `DEPENDENCIES.md` claims they are "pinned by `uv.lock`",
  which is false for a consumer installing the wheel.
- **Finding 8.** `pypdf` entered `pyproject.toml` inside the tag and is absent
  from `DEPENDENCIES.md` (AGENTS.md rule 6 violation). Maintainer ruling: it is
  an **example** dependency, not a `legio` dependency.

## Scope

1. **Cut `v0.1.1`** from the corrected tip (the maintainer chose not to rewrite
   the published `v0.1.0`). Steps: land LEG-104/105/106 + this slice, re-run
   `make validate-release`, add the missing changelog entries, bump
   `pyproject.toml`/`Makefile`/`__version__` to `0.1.1`, tag, re-run the
   fresh-clone reproducibility audit.
2. **Changelog**: `[0.1.1]` documents LEG-104..109 and any previously untagged
   work; `[0.1.0]` is corrected to note LEG-063/064 were inside the tag.
3. **Release guard**: `make release` (and the release doc) refuses a dirty tree
   or a validation record older than `HEAD`, so the tag can never again certify
   an unvalidated tree.
4. **Dependency bounds** (requires maintainer approval, rule 6):
   `beaver-db>=2.4,<3`, `lingo-ai>=2.1,<3`.
5. **`pypdf`** removed from `legio`'s `[dependency-groups].dev`; it lives with
   the example (`examples/document_processing/requirements.txt`). `DEPENDENCIES.md`
   gains a row marking it **example-only, not a legio dependency**, and the
   `beaver-db`/`lingo-ai` sentence is corrected.

## Non-goals

- No re-tagging of `v0.1.0`.
- No publish workflow (separate maintainer follow-up).

## Validation

- `make validate-release` green on the final tree; record timestamp after
  `HEAD`.
- Fresh clone at the new tag: `make ci` green, `make build` + `make
  validate-release` green.
- `grep -n "LEG-063" CHANGELOG.md` non-empty.
