# AGENTS.md — Working rules for implementing agents

This file is the operational contract for every coding agent that works on
`legio`. Read it fully before doing anything. It is the guiding checklist for
implementing agents.

## Non-negotiable rules

1. **English only.** All code, identifiers, comments, docstrings, documentation
   and commit messages are written entirely in English. Repo content is
   self-contained in English.
2. **Semantic-informative naming.** Every name carries its meaning:
   - Functions/verbs, classes/nouns, booleans read as predicates.
   - Prefer explicit over clever/abbreviated. No `x`, `tmp`, `foo`.
3. **Journal and commit it every turn.** Two things persist *separately*:
   - **The journal (fixed, mandatory, every turn).** No turn ends — even
     mid-session (a session may span several turns; each appends chronologically
     to the same day file) — until the journal under `docs/JOURNALS/` records
     that turn **and that journal is committed within the turn itself**, whether
     or not any code changed. The entry serializes *what was discussed and
     decided*: what was done (per issue), decisions taken, open questions, tests
     run/passed, known issues, and next steps. It reflects the real tree as of
     the end of the turn and must not "look ahead" to describe uncommitted future
     work as done.
   - **The work tree (not fixed per turn).** The code/docs a turn touched is *not*
     obliged to be committed in that same turn: it may be a decision in progress
     that the next turn will reshape. The work is committed on its own, when it
     becomes final/stable. The only commit that is fixed each turn is the
     journal's.
   Before starting any work, read the latest journal entry — it is how
   development resumes.
4. **Build and test what you do.** No code change ships without tests that run
   and pass in CI (`ruff`, tests, typecheck). "Done" always includes green tests.
5. **Plan → specs → implementation; work is per issue.** Work follows the
   issues in `docs/PLAN.md`; each GitHub issue carries its spec (linked in the
   issue body, file under `docs/CONTRACTS/LEG-xxx-*.md`). Implementation of an
   issue requires its spec to be approved first (contract-first, TDD). The
   main branch advances one issue at a time; only the maintainer closes an
   issue, after approval.
6. **Dependency discipline.** Only the dependencies listed in
   `docs/DEPENDENCIES.md` (once approved) may be used. Never add, upgrade or
   guess a dependency without explicit approval. `uv` manages the environment.
7. **Domain-free library.** `legio` must never know about any consumer domain
   (audio, images, CRM, ETL...). It only sees: patterns (YAML as data)
   and the injected tool registry. No consumer code, names or data inside this
   repo — not even as examples or validation material.
8. **Polling only.** The public API never emits callbacks or push events.
   Nothing sleeps; scheduling is a field (`next_run_at`).
 9. **Errors are never silent.** Failures are visible in registries and in the
    task result. Never swallow exceptions.
10. **Before coding, read** `docs/ARCHITECTURE.md`, `docs/CONTRIBUTING.md`,
    the relevant section of `docs/PLAN.md`, and the latest journal.
11. **Log every observable event.** legio is a concurrent, decoupled engine; it
    must be observable at runtime. Every module provisions its own logger
    (`logging.getLogger(__name__)`, under the `legio.*` tree) and emits
    structured `key=value` events (INFO on decisions/deposits, DEBUG on step
    detail, WARNING on failures/rejections/denials, `logger.exception` on
    crashes). legio never configures the root logger — consumers own logging
    config via `legio.logging.configure()`. Logging is added *with the
    implementation*, not retrofitted afterwards.
12. **Implement to the design, then re-verify.** Before coding an issue, anchor
    to the designed specs and `docs/ARCHITECTURE.md` — do not improvise new
    behavior. After implementing each spec/issue, re-audit the shipped code
    against the designed architecture and its contract before marking it done;
    deviations must be fixed or explicitly documented as debt. This prevents
    regressing the decoupled/polling model (see the Session 7 audit).

## Definition of done (per issue)

- Contract tests written first (red), implementation then (green).
- Change verified against the corresponding validation case.
- Module logging is in place at every observable lifecycle point (rule 11),
  shipped *with* the implementation, not retrofitted.
- No regressions: full test suite + lint + typecheck pass.
- Journal updated (reporting logging events added). Docs updated if a contract
  or API changed.

## Journaling template

Use `docs/JOURNALS/0000-TEMPLATE.md`. Filename: `docs/JOURNALS/<YYYY>-<MM>-<DD>.md`,
append chronologically to the same day file when working multiple turns a day.