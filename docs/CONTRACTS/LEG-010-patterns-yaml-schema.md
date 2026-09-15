# LEG-010 — Patterns YAML schema (S1)

- **Status:** APPROVED (S1 — one agent spec; see `docs/AGENT_LIFECYCLE.md`
  §4.10/§4.11)
- **Rasante:** R-1 (contract)
- **GitHub issue:** #4
- **Source:** `docs/PLAN.md` (LEG-010)
- **Depends on:** H1–H4 findings in `docs/VALIDATIONS/single-node-model.md`

## Goal

Define the S1 YAML schema for patterns. Patterns are **YAML data** (rule
7) defining **agents**: one single agent specification shared by every agent
(`type: atomic | composite`; `kind: tool | linguistic` for the atomic interior),
with **mandatory symmetric entry/output contracts** (full triples on every
agent, no exceptions), a **terse call vocabulary** for tools (`parameters:
{arg: {input_as}.{key} | literal}` — no `{from:}`/`bind:`/`emit:`/`children`),
chain-wide composability verification by **contract compatibility**, reuse of
any definition by name and by repetition (position), and `main` as a root
capability. A composite is `type: composite` + `branches` (no
`sequence`/`parallel` kinds); a step in `branches` is a **bare pattern name** —
the `(class, input_as)` routing pair is resolved by the loader (Schema 2), not
declared in the agent. The full normative statement lives in
`docs/AGENT_LIFECYCLE.md` §4.10 / §4.11 Schema 1.

## Scope

- **In scope:** the one agent spec (§4.10.1); branch-exclusive structural
  enforcement (no inference); mandatory symmetric contracts with
  `input_as`/`output_as` (the anti-convention rule, §4.10.2) and contract
  compatibility as the composition relation; the terse call
  vocabulary with explicit `{input_as}.{key}` resolution (§4.10.3); interior ↔
  contract coherence (tool ↔ code signature; linguistic prompt variables ↔
  `input_schema`; linguistic `output_schema` enforced at runtime) §4.10.4;
  reuse of every definition by name / repetition and encapsulation (§4.10.5);
  composite `branches` (steps are bare pattern names; no inline, no nested
  `branches`) and composite output without `emit:` (the combination is the
  agent's implementation, §4.10.6); `main` as root capability not position
  (§4.10.7); load-time composability verification (§4.10.8).
- **Out of scope:** the tool registry + policy (Schema 3 / `available_tools`);
  the message/token envelope (Schema 2) — including the resolved
  `(class, input_as)` `level_route` (LEG-011); the loader that resolves the DAG
  of each level/branch (LEG-021); materialization artifacts (queue names,
  compiled pydantic models, derived identities); the `output_schema`
  mini-grammar (defined by LEG-072 compilation semantics); the dry-run
  validator (LEG-071).

## Contract & design

### The one agent spec (§4.10)

```yaml
type: atomic | composite                    # root discriminator; exactly these two
kind: tool | linguistic                     # interior of ATOMIC only (composites are type: composite)
name: <id>                                  # MANDATORY, every agent
description: <text>                         # optional
main: bool                                  # enables being a starting agent (eligibility, NOT position)

# Entry contract — MANDATORY for every agent
input_as: <alias>                           # names THIS agent's incoming payload space
input_type: text | json | binary
input_schema:                               # mandatory (json: object; text has none)

# Output contract — MANDATORY for every agent
output_as: <alias>                          # names THIS agent's deposited payload space
output_type: text | json | binary
output_schema:                              # mandatory (json: object; text has none)

# Interior — ATOMIC only, by kind
tool: <available_tools-key>                 # kind: tool; the CALL + the code's signature
parameters: { <arg>: <{input_as}.{key} | literal> }   # kind: tool; explicit {input_as}.{key}, no {from:}/{value:}/{default:}
prompt: "<template with {var}>"             # kind: linguistic

# Interior — COMPOSITE only (type: composite)
branches:                                   # list of branches; each branch an ordered list of PATTERN NAMES
  - - <pattern-name>                        #   step: a BARE reference to a defined agent — by name only
    - <pattern-name>                        #   a composite-name step = inner ramification (reuse by ref)
  - - <pattern-name>
    - <pattern-name>
```

### Rules (normative subset)

- **Discriminators:** `type` discriminates; `type: atomic` → `kind:
  tool | linguistic`; `type: composite` → `branches` and no `kind`. Crossed or
  missing `kind`, missing `name`, both work keys on one atomic,
  `parameters`/`prompt`/`branches` on a composite, composite without `branches`,
  atomic with `branches`, a schema on a `text` type, a tool without `tool`, a
  linguistic without `prompt` → parse error.
- **Contracts:** entry + output **mandatory for every agent** — the full triples
  (`as`/`type`/`schema`), strict symmetry, no exceptions (even composites
  declare `output_schema`); `input_type`/`output_type` decide where a value
  lives (text/json/binary; text has no schema); schemas validate at the
  boundary (rule 9). An agent's `input_as`/`output_as` name **its own** payload
  space; they are not routing fields.
- **`input_as`:** reserved words are forbidden as a read root; every read starts
  from a declared alias.
- **Terse call vocabulary:** a tool `parameters` value is **exactly one of** an
  **explicit `{input_as}.{key}` dotted path** (the agent's own declared
  `input_as` + a key of its `input_schema`) or a literal; there is no
  `{from:}`/`{value:}`/`{default:}` wrapper; the default lives in the code's
  signature and is never written. Resolution is explicit and verbose — never
  implicit against the input content.
- **Contract compatibility:** a consuming step's entry contract must be
  satisfiable by the producer's output (the fields it consumes exist with the
  promised types); the step that produces only `{body}` cannot feed a step whose
  contract demands `{url}`.
- **Coherence:** tool `input_schema` ⊆ the registered code signature (checked at
  load); linguistic template `{var}`s ↔ `input_schema` (all used, all
  declared); linguistic `output_schema` enforced at runtime.
- **Steps reference, they never define:** a step in `branches` is a **bare
  pattern name** — a reference to a defined agent; no inline definition, no
  nested `branches`, and **no `input_as`/`output_as` written on the step**. The
  `(class, input_as)` routing pair is **resolved by the loader** when it builds
  the DAG of each level/branch (Schema 2, LEG-011); it is never declared in the
  agent definition.
- **Federated resolution (amended by LEG-094, session 86e):** on a federated
  boot the step's universe is the **local catalog ∪ the configured peers'
  rosters**. A branch step resolving against a peer's roster becomes a *delegated
  step*: its `(class, input_as)` pair uses the peer's advertised `input_as`
  (LEG-090 wire) and its queue is routed to the owning peer by the `NodeDB`
  (LEG-095 Phase 3). An unknown-everywhere step is a visible load/boot error as
  before (rule 9); single-node catalog loads keep the exact LEG-010 semantics.
- **Reuse:** definitions are source-agnostic; a composite references an agent by
  name as a step; the same agent may appear more than once in a flow **by
  position** (distinguished by `current_index`), unless inside its own
  definition (cycle → infinite recursion, rejected); a use satisfies the used
  agent's entry contract (subset of declared properties, `required` covered or
  defaulted, extras = load error).
- **Encapsulation:** interior opaque; `output_as` unique per scope; composition
  cycles rejected at catalog load.
- **Composite output (no `emit:`):** how a composite combines its children's
  results into its declared `output_as`/`output_schema` is **its
  implementation**; there is no "last child renamed" and no `emit:` map. A
  composite's steps bind from the composite's `input_as` (the loader resolves
  each step's `(class, input_as)`); inner ramification is a step whose name
  references a `type: composite` agent (no nested `branches`).
- **`main`:** root capability (submittable, seeds the flow in level 1 with
  `end_of_level_queue` = the final-result queue), reusable mid-DAG (referenced
  by name as a step); no effect on contracts or the calling mechanism.
- **Load-time composability:** every invalid spec fails at load (LEG-071).

## Interface

- The YAML shape above, plus the validation matrix (R-3 fixture set).

## Acceptance criteria

From `docs/PLAN.md` (LEG-010), reworked for S1:

- A fixture translating two representative composite patterns into S1 YAML
  loads and validates: (a) a composite with a single branch (a sequence of
  steps) reusing a tool node and a linguistic node; (b) a composite with two
  branches.
- A prompt with `{input_var}` variables fills from the entry contract aliases;
  a `{var}` not declared in `input_schema` is a load error.
- A tool `parameters` dotted path is explicit `{input_as}.{key}` and a missing
  `input_as`/`key` (not in the agent's own `input_schema`) is a load error.
- The contracts are **mandatory**: a node missing `input_as`/`input_type`/
  `input_schema` or `output_as`/`output_type`/`output_schema` is rejected.
- Steps in a composite are **bare pattern names**: a step carrying an
  `input_as`/`output_as`, an inline definition, or a nested `branches` is
  rejected.
- Reuse: the same agent definition used in two composites validates in both; a
  use that fails to satisfy the used agent's entry contract or adds an
  undeclared key is rejected; repeated identical steps only chain when
  contract-compatible.
- Coherence: a tool whose `input_schema` does not match its registered
  signature; a linguistic with an unused/undeclared prompt variable; and a
  linguistic output that violates its declared `output_schema` at runtime —
  all produce detectable errors (load-time or rule-9 visible).
- Encapsulation/cycles: duplicate `output_as` in one scope and composition
  cycles are rejected at load.

## Tests

- Contract tests (red first, R-3 loader): S1 fixtures (two composites),
  the explicit-`{input_as}.{key}` `parameters` vocabulary, reuse, branch-exclusive
  matrix, mandatory-contract matrix, bare-pattern-name step rejection, coherence
  checks (tool signature vs `input_schema`; linguistic prompt variables),
  composite output via implementation, duplicate
  `output_as`, cycles — all validated against the schema; linguistic runtime
  `output_schema` enforcement via MockLLM.

## Validation case

- Two in-repo example composites, re-expressed in the S1 shape (R-2/R-4
  conformance fixtures), plus a multi-step-branch fixture that exercises inner
  ramification via a composite-in-branch reference.

## Definition of done

- All acceptance criteria met by running checks (`ruff`, `pytest`, `pyright`).
- Maintainer approval recorded; the maintainer closes the GitHub issue.
- Journal entry appended.
