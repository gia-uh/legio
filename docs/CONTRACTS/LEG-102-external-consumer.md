# LEG-102 — External consumer pins the released legio

- **Status:** APPROVED by maintainer direction on 2026-09-21 (GitHub #47),
  with an in-repo amendment: the *external consumer repo* (an own repository
  pinning the release) stays the maintainer's follow-up (no `gh` repository
  exists yet and the package is not published); the implemented scope is the
  **in-repo executable proof** — `make validate-release` installs the built
  wheel into a throwaway venv and runs a headless, domain-free smoke against
  the installed artifact, recording the pinned version.
- **Closed:** 2026-09-21, GitHub #47 closed as implemented (`7b6774c` +
  `scripts/validate_release.sh` record in `docs/VALIDATIONS/`); the external
  consumer repo stays the maintainer's follow-up.
- **Rasante:** R-10
- **GitHub issue:** #47
- **Source:** `docs/PLAN.md` (LEG-102)
- **Depends on:** LEG-101

## Goal
Prove the released package against a real consumer in its own repository (kept
separate — never consumer material in `legio`), pinning the released version
with its validation suite green.

## Scope
- **In scope:** consumer repo pins `legio==0.1.0`; its validation suite green.
- **Out of scope:** any consumer material inside `legio` (per AGENTS.md rule 7).

## Contract & design
- A separate consumer repository depends on the published `0.1.0` and runs its
  own validation suite against it (in editable mode during development is
  allowed, but this issue validates against the released artifact).
- Traceability: the consumer records the exact pinned version.

## Interface
- Dependency pin `legio==0.1.0`.

## Acceptance criteria
From `docs/PLAN.md` (LEG-102), verbatim:
- The consumer repo pins the released version; its validation suite runs green
  against it.

## Tests
- In-repo harness (red first): `scripts/validate_release.sh` + the documented
  `make validate-release` — throws a venv away, installs `dist/*.whl`, runs a
  headless smoke (import `legio`, `legio.__version__ == 0.1.0`, boot a
  domain-free node and submit → status round-trip over a headless consumer
  using the `examples/` single source), records the pinned version in
  `docs/VALIDATIONS/`.
- Maintainer follow-up (out of repo): a consumer repository pinning the
  published release with its own validation suite.

## Validation case
- `make validate-release` against the freshly built wheel (the executable
  proof appended to the journal: pinned version + smoke result).

## Definition of done
- All acceptance criteria met by running checks.
- Maintainer approval recorded; the maintainer closes the GitHub issue.
- Journal entry appended.