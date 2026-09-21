# LEG-100 — Docs & examples hardening; glossary; consumer guide

- **Status:** APPROVED (maintainer review 2026-09-21)
- **Closed:** 2026-09-21, GitHub #48 closed as implemented (`f580a1a`).
- **Rasante:** R-10
- **GitHub issue:** #48
- **Source:** `docs/PLAN.md` (LEG-100)
- **Depends on:** R-0..R-9 outputs

## Goal

Hardening pass: executable consumer guide (adding tools + patterns to a node),
real glossary, and every in-repo example a green test (no bitrot).

## Scope

- **In scope:** consumer guide, glossary, examples-as-tests hardening, README
  refresh (initial version of `README.md` describing the current state and
  docs surface).
- **Out of scope:** semantic versioning/release (LEG-101); the `legio
  validate --dry-run` CLI (GitHub #38, withheld — validation is documented as
  the boot-time fail-fast refusal that exists today).

## Contract & design

Decisions recorded 2026-09-21 (maintainer):

1. **Examples single source.** The four canonical domain-free flows
   (`transform`, `summarize`, `extract_and_summarize`, `distribute_summary`)
   live as self-contained **example nodes** under `examples/`, each with its own
   `patterns/` (Schema 1 YAML), `tools.yaml` (Schema 3) and `legio.yaml`
   (LEG-017), plus shared reference tool implementations in `examples/tools.py`.
   The example tests (`test_leg032`, `test_leg043`) and the consumer-guide
   walkthrough test load **the same files** — any drift in a documented
   example breaks the suite.
2. **Consumer guide shape.** `docs/CONSUMER_GUIDE.md` as markdown; the CI
   proof is `tests/test_leg100_consumer_guide.py`, which walks the guide
   literally: every example node's patterns load and validate, the example
   `legio.yaml` templates parse, and the headless `transform` node boots and
   serves a submit → status round-trip over the REST surface.
3. **Validation step.** No new validator command. The guide documents the
   existing fail-fast boot refusal (`legio server` refuses the boot naming the
   invalid pattern / tool / config before any agent binds — rule 9).
4. **Glossary.** `docs/GLOSSARY.md`: one canonical definition per term —
   LEG-016 identifiers, the three schemas, architecture concepts and the error
   taxonomy — anchored to the code and architecture docs.
5. **README.** `README.md` refreshed to the current state (R-10 hardening
   under review; examples + glossary + consumer guide referenced in layout).

Every example stays domain-free (AGENTS.md rule 7): generic operation names
only, no consumer-domain vocabulary or data.

## Interface

- `examples/` — the domain-free example-node tree (README, `tools.py`, per-node
  `patterns/` + `tools.yaml` + `legio.yaml`).
- `docs/CONSUMER_GUIDE.md` — the executable walkthrough.
- `docs/GLOSSARY.md` — the canonical glossary.
- `README.md` — refreshed top-level entry point.

## Acceptance criteria

From `docs/PLAN.md` (LEG-100), verbatim:

- Consumer guide (adding tools + patterns to a node) is executable
  top-to-bottom; examples are all green tests.

## Tests

- `tests/test_leg100_consumer_guide.py` — the walkthrough (guide executable).
- `tests/test_leg032_example_summarize.py`, `tests/test_leg043_examples_composites.py`
  — refactored to load their patterns from the example-node files (single source).

## Validation case

- The `transform` example node boots headless and serves submit → status
  (the executable core of the guide); the three composite/linguistic example
  nodes are exercised end-to-end by the refactored example tests over the same
  files.

## Definition of done

- All acceptance criteria met by running checks: full suite + ruff + pyright
  green.
- Maintainer approval recorded; the maintainer closes the GitHub issue.
- Journal entry appended.