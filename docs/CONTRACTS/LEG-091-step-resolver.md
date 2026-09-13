# LEG-091 — Step resolver: required agent → local | remote

- **Status:** spec approved (maintainer, session 85r — federation-first reprioritization).
- **Rasante:** R-9
- **GitHub issue:** #43
- **Source:** `docs/PLAN.md` (LEG-091)
- **Depends on:** LEG-090 (capacity + roster), LEG-016 (error taxonomy),
  `flow/messages` (`SCHEMA_VERSION`).

## Goal

The author resolves each step's required agent **before deposit**: local when
the node serves it, remote when a configured peer's catalog offers it with a
matching `schema_version`, error otherwise. Nothing unresolvable is ever
deposited — a pre-deposit gate.

## Problem (modern-triangle mapping)

The DRAFT referenced the LEG-015 in-memory `Federation` and its module-global
`_NODES`. In the modern architecture (ARCH §9, verified this session — the
maintainer's local-first framing):

- **Local capacity** = the node's served pattern catalog
  (`pattern_catalog.served()`, the same capacity LEG-090 exposes).
- **Peer catalogs** = the `GET /catalog` responses (LEG-090) of configured
  peers, fetched by the author-side work-item client (LEG-092).
- **Lifecycle stays local**: federation only transports *work* (submits),
  never agent control — a node cannot enable/disable/destroy another node's
  agents (enforced doubly: the federation surface has no lifecycle endpoint,
  and control messages are signed with a per-boot key that never leaves its
  node, LEG-082).

The resolver is therefore a **pure decision** — no beaver, no HTTP, no domain
knowledge (rule 7): it consumes rosters and yields a decision that a depositor
(LEG-092) later executes. It is the "resolution happens before deposit" gate of
ARCH §9: an acceptor's agents never receive work for a pattern they do not
serve.

## Scope

- **In scope:** the pure resolution logic (local/remote/error), the pre-deposit
  gate, the module `legio.federation`.
- **Out of scope:** HTTP authority-side transport (fetch peer catalogs, deposit
  work items — LEG-092); the acceptor `POST /work-items/{agent}` (LEG-092);
  author outbox (LEG-093); any lifecycle endpoint (local by design).

## Contract & design

- `StepResolver(local_capacity: frozenset[str], peer_catalogs:
  Mapping[peer_id, Mapping[agent, interface]])` — the local capacity and the
  current peer rosters are injected (the client layer owns fetching).
- `resolve(agent) -> Local | Remote`, deterministic order:
  1. `agent in local_capacity` → `Local(agent)`.
  2. else, in the given peer order, the **first** peer whose catalog offers the
     agent decides: matching `schema_version` → `Remote(peer_id, agent,
     interface)`; a mismatched `schema_version` → visible `InterfaceMismatchError`
     (rule 9 — never a silent skip to the next peer).
  3. no peer offers the agent → `UnresolvableAgentError` (LEG-016 taxonomy).
- Resolution is a pure read: it never deposits, never mints, never touches any
  beaver scope or agent queue (the pre-deposit gate is *the resolver itself
  raising* before any deposit can happen).

## Interface

- `resolve(agent) -> Local | Remote`
- Errors (LEG-016, rooted in `LegioError`): `InterfaceMismatchError` (peer
  offers the agent with an incompatible `schema_version`),
  `UnresolvableAgentError` (served neither locally nor by any peer).

## Acceptance criteria

From `docs/PLAN.md` (LEG-091), verbatim:
- Author resolves each step locally when present, remote when the peer catalog
  offers it, error otherwise; resolution happens before deposit.

## Tests (red-first)

- Local when served.
- Remote via a peer with a matching `schema_version`.
- A peer offering the agent with a mismatched `schema_version` fails loudly
  (no silent skip to a later peer).
- Served neither locally nor by any peer → `UnresolvableAgentError`.
- A peer that does not offer the agent is simply skipped (continues to the next
  peer).
- The resolver is pure: resolving never deposits or touches any queue.

## Validation case

- LEG-094 multi-node example (remote delegation).

## Definition of done

- All acceptance criteria met by running checks (ruff + pytest + pyright green).
- Maintainer approval recorded; the maintainer closes the GitHub issue.
- Journal entry appended; the LEG-091 work tree commits when stable.