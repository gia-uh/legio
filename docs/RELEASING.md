# Releasing legio (LEG-101/LEG-102)

The release track is **maintainer-only**: an agent implements and verifies the
in-repo steps below; the maintainer performs the external steps (registry
publish, closing the GitHub issues).

## Versioning

Semantic versioning (`docs/CONTRACTS/LEG-101-semver-release.md`). The single
version source is `pyproject.toml` (`[project].version`); the package mirrors
it in `src/legio/__init__.py` (`__version__`) and the Makefile in `VERSION`.
A release bumps all three together — a mismatch is a loud, breaking error to
catch in review.

Current version: `0.1.0`.

## Release process

The whole release is a sequence of already-green gates plus three actions
(build, changelog, tag), each repeatable:

1. **Gate:** `make ci` — lint (ruff check) + format check + typecheck
   (pyright) + the full test suite, all green on `main`.

2. **Changelog:** `CHANGELOG.md` gets a `[<version>]` section listing every
   merged issue since the previous release (source of truth: `docs/PLAN.md`
   issue list + the git log). If this section exists for the version being
   cut, it is already done.

3. **Build the artifacts:** `make build` (runs `uv build`). This produces the
   wheel and sdist under `dist/` (gitignored; regenerable).

4. **Validate the artifact (LEG-102):** `make validate-release`. This
   rebuilds the wheel, installs it into a throwaway virtualenv, and runs a
   headless, domain-free smoke against the *installed* package — import,
   `__version__`, and a boot + submit round-trip — recording the pinned
   version in `docs/VALIDATIONS/`. The real external-consumer repo (an own
   repo pinning the release) remains a maintainer follow-up; the in-repo
   harness is the executable proof of the artifact.

5. **Tag:** `make tag` — `git tag v<VERSION>` on the release commit (the
   commit that carries the bump, the changelog entry and the validation
   record).

6. **Publish (external, maintainer):** upload the artifacts from `dist/` to
   the package registry and close the GitHub issues (`#45`, `#47`).

## Definition of done (LEG-101/LEG-102)

- `uv build` produces wheel and sdist; `dist/*.whl` and `dist/*.tar.gz` exist.
- `git tag v<VERSION>` exists on the release commit.
- `CHANGELOG.md` lists every merged issue (parity with the PLAN issue list).
- `make validate-release` passes against the installed artifact and the
  pinned version is recorded.
- Journal entry appended (this turn), the journal commit and the release
  commits made, per `AGENTS.md` rule 3.