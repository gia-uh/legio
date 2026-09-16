# CONTRIBUTING — Methodology and development flow

This document is the methodology for building `legio`. Complement to
`AGENTS.md` (operational rules) and `docs/PLAN.md` (issues).

## 0. The unit of work is a GitHub issue

Every GitHub issue in the repo (LEG-xxx) comes from `docs/PLAN.md`. Development
is done **one issue at a time**, strictly in this order:

1. **Spec first.** No implementation starts without the issue's spec, written
   in `docs/CONTRACTS/LEG-xxx-*.md` and approved. The GitHub issue links to its
   spec file (field "Spec"). R-1 (LEG-010..017) are the contracts that the rest
   of the issues reference.
2. **Implement the issue.** Contract tests first (red), implementation second
   (green), against the issue's acceptance criteria.
3. **Validate.** The issue's validation case is green (no real network/LLM).
4. **Review + approve.** The maintainer reviews; after approval the issue is
   closed **by the maintainer only** — never by the implementation, never by CI.
   The closing comment records the checks that were run.
5. **Journal.** Every session appends to `docs/JOURNALS/`.

The issue is the source of truth for its own status: it points to its spec, its
PR, and (when closed) the verification that was run.

## 1. Model: vertical slices with dogfooding

Each increment = **one capability of `legio` + the real validation case that
exercises it** (an independent consumer in its own repository, kept separate).
Hard rule:
*a feature is not closed if there is no real case in green exercising it.*
`legio` itself never ships consumer material — only in-repo fictitious-domain
examples. Consumer names, patterns and data never enter this repository.

Work order per slice: write contracts/specs → agree them → implement →
validate → journal.

## 2. Contract-first, TDD

1. **Tests fix the contract first** (red), implementation second (green). The
   contract tests *are* the specification.
2. Substitutes in tests: LLM → `MockLLM` (lingo); tools → fakes / `respx`;
   beaver → temporary file; inter-node HTTP → `respx`.
3. CI never touches real networks or real LLMs.

## 3. Validation and dogfooding

- Inside `legio`, the **examples are tests** (they must not bitrot): a minimal
  fictitious domain exercising each slice without depending on any consumer.
- A consumer repository depends on `legio` in editable mode during
  development; each `legio` release ships with semver and that consumer's
  validation case in green pinned to the exact version.
- "Done" for a slice: written contract + contract tests + implementation +
  validation cases green + docs.

## 4. Git, CI and releases

- Short-lived branches + PR (trunk-based). Conventional commits. English, in
  code as in commits.
- CI runs: `ruff` lint, typecheck, full test suite. Test failure paths
  explicitly (never-silent errors, priority, cascade invalidation, partial
  failure).
- Strict semver from `0.1`; changelog and tags per release; each release is
  justified by its validation case.

## 5. Contracts and documentation

- Contracts change **only in writing**: versioned documents under
  `docs/CONTRACTS/` plus an ADR for each change; changes go through reviewed
  PRs.
- Minimal docs: README, ARCHITECTURE, PLAN, DEPENDENCIES, JOURNALS, glossary,
  and a guide for consumers (how a domain adds tools + patterns).

## 6. Design rules (non-negotiable)

1. **Zero domain dependencies**: `legio` never imports or knows consumer tools
   or patterns; only interfaces (injected registry, YAML as data).
2. **No global state**: explicit configuration through a `Manager`/`Engine`
   object (explicit over implicit) → testable.
3. **Polling only**: the public API exposes no callbacks or events; nothing
   sleeps — it is a field (`next_run_at`).
4. **Messages and token immutable and typed** (pydantic), with `schema_version`.
5. **Errors are never silent**: visible in registries + result with error;
   swallowing exceptions is forbidden.
6. **Namespacing of the names legio owns**: the only invented namespace is the
   per-agent queue (`legio:queue:<agent>`, via `legio.naming.queue_key`);
   registries are beaver dicts addressed by their scope name directly
   (`db.dict(scope)`), so domains/nodes sharing a beaver do not collide on
   scopes.
7. **Concurrency**: beaver locks with TTL + `renew` used only where genuine
   mutual exclusion over a shared key is required; per-resource semaphores;
   replicas never reach consensus through their own process state.
8. **Single domain extension point**: the tool registry. Everything else is
   closed.
9. **Strict validation**: startup with cascade invalidation — a broken YAML is
   rejected, not ignored.

## 7. Review checklist (per PR)

- Does it introduce domain or global state? → no.
- Does it have contract tests fixing behavior? → yes.
- Is the real validation case green? → yes, except foundation.
- Are errors visible and never swallowed? → yes.
- Docs/contract/ADR updated if a contract changed? → yes.
- Journal updated describing what was done and what ran? → yes.
- Naming semantic-informative, English only? → yes.