# LEG-116 — Release validation is `uv publish --dry-run`, not a global `twine`

- **Status**: APPROVED (maintainer direction 2026-09-28: "haz 2 para que no siga
  pasando").
- **Type**: process/docs fix — GitHub issue #66.
- **Related**: LEG-101 (release), LEG-102 (validation), LEG-107 (release guard).

## Problem

The release validation reached for a globally-installed `twine check`, which
reported a **false** error:

```
InvalidDistribution: Metadata is missing required fields: Name, Version.
```

Root cause: the wheel/sdist carry `Metadata-Version: 2.4` (PEP 639, from
`license`/`license-files`); `twine` 5.1.1 bundles `pkginfo` 1.10.0, which does
not understand 2.4 and parses `name=None`/`version=None`. Verified locally:
`pkginfo 1.10.0 → name=None`; `pkginfo 1.13 → name='legio', version='0.1.2'`.
The metadata is correct; the parser is stale. `twine` is neither a project
dependency nor used by CI, so the check depended on an unknown global version.

## Contract

- The release validation is **`make publish-check`** = `uv publish --dry-run` on
  the `$(VERSION)` `dist/` artifacts — the real validator, no external tool.
- `RELEASING.md` documents it and warns against a global `twine check`.
- No `twine` dependency is added.

## Acceptance

- `make publish-check` builds nothing that is not built and validates the
  `$(VERSION)` wheel + sdist against PyPI without uploading.
- `RELEASING.md` names `make publish-check`; the release track comment in the
  `Makefile` includes it.
- Full gate green.

## Out of scope

- Upgrading or vendoring `twine` (removed from the process).
