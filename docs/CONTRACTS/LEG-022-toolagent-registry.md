# LEG-022 — ToolAgent (Schema 1/2/3)

- **Status:** APPROVED (Schema 1 pattern + Schema 2 token + Schema 3 tools)
- **Rasante:** R-2
- **GitHub issue:** #14
- **Source:** `docs/PLAN.md` (LEG-022)
- **Depends on:** LEG-013 (Schema 3), LEG-011 (Schema 2), LEG-010 (Schema 1)

## Goal
Implement the ToolAgent execution path: a `kind: tool` agent that resolves its
terse `parameters` against the incoming payload, invokes the bound
`tool: <name>` (a Schema 3 resource), validates the contracts on the edges, and
advances the route by position (Schema 2).

## Scope
- **In scope:** the ToolAgent execution path (parameters resolution → call →
  contract validation → route advance / deposit).
- **Out of scope:** runtime tool-loading mechanics (decided at implementation,
  not the schema).

## Contract & design
- A `kind: tool` agent (Schema 1) declares `tool: <name>` (a Schema 3
  `available_tools` key) and `parameters: {arg: dotted.path | literal}` — the
  **terse call** (no `{from:}`/`{value:}`/`{default:}`; the default lives in the
  code's signature and is never written).
- Flow: take the incoming payload (`request.payload`, Schema 2) → resolve each
  `parameters` value (dotted path against the chain-in-scope, or a literal) →
  invoke the bound tool's callable with those kwargs → the tool's contract
  (signature/parameters) is validated **at execution time** → build the new
  payload → the base routes by position (Schema 2): advance to the next class
  of the level, or deposit to `end_of_level_queue` at level close / flow end.
- A mismatch (missing `parameters` source, signature rejection) is a **visible
  execution error** (rule 9), never silent.

## Interface
- `ToolAgent(...)` runner over `AgentBase` (Schema 2 routing), bound to a
  `kind: tool` spec and a Schema 3 tool resource.

## Acceptance criteria
From `docs/PLAN.md` (LEG-022), verbatim:
- A `kind: tool` agent executes against its `tool: <name>` in `available_tools`,
  resolving `parameters` (dotted paths/literals) into the call, validating
  input/output contracts on the edges, advancing the route by position and
  depositing the new payload; failures yield a visible error in the task
  result.

## Tests
- Contract tests (red first, once S1/S3 migration begins): parameters
  resolution (dotted path + literal), happy path, contract/signature rejection
  on both edges, route advance by position.

## Validation case
- `transform` fake tool (declared via `available_tools`) end-to-end.

## Definition of done
- All acceptance criteria met by running checks.
- Maintainer approval recorded; the maintainer closes the GitHub issue.
- Journal entry appended.