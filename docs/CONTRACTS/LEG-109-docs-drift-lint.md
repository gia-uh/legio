# LEG-109 — Documentation drift and doc-lint

- **Status:** APPROVED by maintainer direction on 2026-09-26 (GitHub #58).
- **Rasante:** R-10.x (hardening of the released `v0.1.0`)
- **GitHub issue:** #58 (findings 9/10 of the #52 audit umbrella)
- **Source:** external audit of `v0.1.0` (`fbd787e`), findings 9, 10
- **Depends on:** LEG-100 (docs hardening)

---

## Goal

The documentation makes only claims the code satisfies, and a cheap check
fails the next time a claim drifts.

## Problem (documentation drift)

- **Finding 9.** README `Development` still describes the Session-108
  format/typecheck debt; the gate is fully green (CHANGELOG is right).
- **Finding 10.**
  - FlowToken field list is wrong in README and `ARCHITECTURE.md` §3: both list
    `message_type, payload` (which live on the messages) and omit `root` (which
    the token carries). Served keys: `branch_id, current_index,
    end_of_level_queue, launcher_class, level, level_route, root,
    schema_version, task_id`.
  - README says `examples/` ships "four" nodes; five ship, and
    `document_processing` breaks the kebab-case naming.
  - `serve_forever` docstring contradicts itself (config vs CLI override).
  - `fetch_peer_catalogs` docstring promises a `RecoverableError` naming the
    peer on a malformed roster; the code raises `ValueError`/`KeyError`.
  - A boot that fails on a missing database directory prints a raw
    `OperationalError` traceback while `CONSUMER_GUIDE.md` §4 promises a loud,
    named refusal. (LEG-104 already removes the missing-dir case; this slice
    makes any other db failure name the offender.)

## Scope

1. Fix the five documentation drifts above (README, `ARCHITECTURE.md`,
   `CONSUMER_GUIDE.md`, `src/legio/cli.py` docstring,
   `fetch_peer_catalogs`).
2. Make `fetch_peer_catalogs` raise the promised `RecoverableError` naming the
   peer and URL on a malformed roster (behaviour change, rule 9).
3. Make a boot database failure name the offending `db_path` (no raw
   traceback); align `CONSUMER_GUIDE.md` §4 with reality.
4. Add a **doc-lint test** that fails on the specific drifts a lint can catch:
   the example count equals the number of directories under `examples/`, the
   FlowToken field list in README/`ARCHITECTURE.md` equals the model's fields,
   and the README no longer carries the stale debt paragraph.

## Non-goals

- No general prose linter; only the concrete, checkable claims above.

## Validation

Red-first doc-lint tests fail on the current docs and pass after the fixes;
`fetch_peer_catalogs` malformed-roster test asserts the peer-named
`RecoverableError`; the boot failure test asserts the named `db_path`.
