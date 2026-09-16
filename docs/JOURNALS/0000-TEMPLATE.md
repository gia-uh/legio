# JOURNALS — entry template

Filename: `docs/JOURNALS/<YYYY-MM-DD>.md`. Append chronologically when working
several turns in the same day. Always fill in English. This is how development
resumes between sessions: the latest entry must say exactly where things stand.

---
# <YYYY-MM-DD> — Session n

## Scope
Issues/slice worked on (e.g. LEG-020..LEG-023, R-2). Goal of the session.

## Work done (per issue)
- **LEG-0xx**: what was built/changed; key decisions.
- ...

## Logging added
- List the logging events emitted with this change (module, level, what they
  cover); per rule 11 logging ships *with* the implementation, not retrofitted.
  If no new events were needed, say so explicitly.

## Tests run
- What was executed (`ruff`, `pytest`, typecheck) and results.
- Which validation case exercises this work (passed?).

## Decisions taken
- Any contract/architecture decision; note if an ADR/doc change is required.

## Known issues / open points
- Remaining work, blockers, questions for the maintainer.

## Next steps
- Concrete next action(s) for the next session.