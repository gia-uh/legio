# LEG-117 — Composite references must not depend on file load order

- **Status**: APPROVED (maintainer direction 2026-09-28: `legio` must be fixed;
  found via invox).
- **Type**: defect fix (patterns loader) — GitHub issue #67.
- **Related**: LEG-044 (nested composites), LEG-021 (loader), LEG-071 (dry-run).

## Problem

`load_pattern_dirs` registers **and validates** each YAML document as it is
read, so a composite whose branch references another composite fails when the
referencing file sorts before the referenced one:

```
patterns reject composite 'generic' branch 0 references unknown pattern: 'text_generic'
```

Renaming the mains so the referenced composites load first makes the same tree
pass (22 specs). Nested composites (LEG-044) must load regardless of file order.

## Contract

- **Two-phase load**: `load_pattern_dirs` / `load_patterns` first **register**
  every spec from every file/dir into the catalog, then **validate** all of them
  once against the complete catalog. Order independence is the guarantee.
- Register-phase failures (shape, duplicate name) and validate-phase failures
  (references, contracts) stay the same loud `UnrecoverableError`s (rule 9).
- The dry-run `validate_pattern_dirs` keeps the same verdict and reports every
  invalid pattern: a register pass (shape/duplicate), then a validate pass
  (references), each finding attributed to its source file.

## Acceptance

- A tree where a composite references a composite defined in a **later-sorted
  file** loads (red-first).
- The dry-run verdict stays equivalent to the boot gate on the existing cases.
- Full suite green.
