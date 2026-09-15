# LEG-094 — Multi-node directed example (3 symmetric nodes, domain-free)

- **Status:** spec approved (maintainer, session 86e — in-session approval, the same
  path LEG-095 Phase 3 took under the R-9 methodology).
- **Rasante:** R-9
- **GitHub issue:** #49
- **Source:** `docs/PLAN.md` (LEG-094)
- **Depends on:** LEG-090 (roster wire, amended below), LEG-091/092/093 (directed
  work/result model), LEG-095 Phase 2 + Phase 3 (per-agent result queue + the
  node-injected db proxy / `POST /deposits`).

## Goal

Proof R-9 in green on the **directed model** (sessions 85w/85y/85z, Phase 3 86d):
3 symmetric nodes share one federation token (L1); author A triggers a 2-level
flow whose branch **delegates a capability to peer B (and C)** and receives the
final result — and, symmetric, B triggers A the same way. Federation only
**transports work** (no lifecycle, no outbox on the target node): every hop rides
one `ExecutionRequestMessage` carrying its own stamped per-hop addressing
(level_route / current_index / end_of_level_queue / branch_id / task_id), the
result returns over the same stamps, and only the author's node writes the
final result.

## Why the directed model (not work-item + target outbox)

The earlier DRAFT (work-item + LEG-093 outbox on the acceptor) belonged to the
pre-Phase-3 federation. Phase 3 (86d) replaced the target-node outbox with pure
transport: the agents' single `db` handle is a `NodeDB` that routes deposits by
queue name (`legio:queue:<agent>` → the owning peer, local-first;
`legio:queue:gather:<agent>` → the owning peer via the same table;
`legio:queue:result:<task>` → the author node parsed from the task id). The
directed example therefore needs **no extra lifecycle verb**: A's flow body
names remote steps exactly like local steps; the `NodeDB` routes the message to
the owning node; the owner's standing loop executes it as a plain foreign
work message; and the produced result is deposited to the *author-owned* queue
the stamps name (`gather:<composite>` — whose owner is the composite's serving
node — or `result:<task>`). Sections §A—§D close the three gaps that stood
between Phase 3 and this picture.

## Gap closure (the contract of this issue)

### §A — The roster advertises each entry's `input_as` (LEG-090 wire amendment)

A delegated step must know the peer's declared **entry alias** to re-key the
payload when the level advances (Schema 2 `(class, input_as)` route - the
`input_as` is *never declared on the step*, LEG-010). There is no local spec
for a peer-offered capability, so the alias must come from the roster:

- `GET /catalog` entries gain `input_as` (the served spec's `input.input_as`).
- `PeerCatalogEntry` gains `input_as`; `fetch_peer_catalogs` parses it; a roster
  entry without `input_as` is a visible `RecoverableError` naming the peer and
  the agent (rule 9).
- The addressing universe of a booted node is: **its own catalog ∪ the
  configured peers' rosters** — the same universe `build_routes` uses (same
  order, so the table and the targeted peer never disagree).

### §B — Federated branch-step resolution (LEG-010/LEG-044 amendment)

Composite `branches` stay bare pattern names; a step now resolves against the
node's **local catalog first, then the peer rosters** (the `StepResolver`
order), wherever the DAG is built:

- **Load** (`_validate_agent_spec` on a federated boot): a branch step that is
  neither in the local catalog nor offered by a configured peer is still a
  visible load error (rule 9). A step offered by a peer loads.
- **Resolve** (`resolve_branch` / `resolve_composite_branches`): a peer-offered
  step resolves to `(step_name, peer_input_as)` from the roster — the branch DAG
  then routes and re-keys exactly like a local step.
- Single-node behavior is **unchanged**: a catalog loaded without peer rosters
  rejects an unknown branch step exactly as LEG-010 pins today.

### §C — Composite dependencies are the local steps (born-enabled, LEG-086/LEG-044 amendment)

`create_class` records a composite's dependencies today as the flattened branch
steps — on a federated node the branches include peer-only capability names that
no local class will ever register, so `dependencies_satisfied` would be
permanently false and the composite would be born disabled (submit denied). On a
node with mounted agents the recorded dependencies are the branch steps the node
serves **locally**; single-node behavior (all steps local) is unchanged. Gates
are unaffected: entry to a remote class is enforced at the remote owner (and
reads open locally by absence).

### §D — The bootstrap seam and transport mesh

- `boot_node` gains `federation_client: httpx.AsyncClient | None`, threaded into
  the `NodeDB` (the remote leg) and into `fetch_peer_catalogs`. Tests inject a
  host-routing ASGI mesh client; live deployments pass nothing (short-lived
  real clients, exactly the Phase 3 seam).
- Roster fetch (injected or live) **precedes** the pattern-catalog load, and the
  derived `peer_steps` feed both the load validation and the branch resolution.
- Operational property (documented, not a bug): a live roster fetch requires the
  peer to already be serving `/catalog` (fail-fast, rule 9). A node that boots
  against peers that are down aborts loudly, naming the peer. New peers are
  brought up first, or the operator injects the rosters (the Phase 3 seam).

## Example topology (in-repo, domain-free — rule 7)

- **A** (`node-a@test`, `http://node-a.test`): `utter` (tool atom) + `audit`
  (composite, `main: true`, `branches: [[spot, refine]]`). Peers: B, C.
- **B** (`node-b@test`, `http://node-b.test`): `refine` (linguistic atom) +
  `brief` (composite, `main: true`, `branches: [[spot, utter]]`) — the symmetric
  flow, delegating to A. Peers: A, C.
- **C** (`node-c@test`, `http://node-c.test`): `spot` (linguistic atom) — the
  shared catalog witness both flows ramify through. Peers: A, B.

Every node serves its own all-local spec set; the *branches* cross the mesh.
`gather:<composite>` is owned by the composite's serving node because the
serving node advertises the composite in its roster and `build_routes` is
local-first; `result:<task>` routes by the task-id author origin.

## Acceptance criteria

From `docs/PLAN.md` (LEG-094), verbatim — mapped to the directed implementation:

- *A triggers a 2-level flow that delegates a capability to B (or C) and
  receives the final result, all with the federation token* — `audit` on A
  fans out branch `[spot, refine]`: `spot` executes on C and `refine` on B
  (foreign `POST /deposits`, L1 bearer), each hop advancing on the message's own
  stamps, and the branch result returns to A's `gather:audit`. A's drain
  completes the task with the built result.
- *symmetric: B can trigger A the same way* — `brief` on B fans out
  `[spot, utter]`: `spot` at C, `utter` at A, result back to B.
- *all with the federation token* — every cross-node hop is a token-guarded
  federation-surface deposit; there is no unauthenticated channel.

## Interface

- `GET /catalog` wire amendment: each agent entry gains `input_as: str`
  (LEG-090, §A).
- `boot_node(..., federation_client=None)` (§D).
- `load_patterns`/`load_pattern_dirs` peer-aware variants (§B).

## Tests (red-first)

1. **The 3-node integration test** (`tests/test_leg094_multi_node_example.py`,
   real beaver, in-process ASGI mesh on `http://node-*.test`):
   - boots A, B and C with injected rosters + the mesh `federation_client`;
   - brings every class up (pumps, standing loops);
   - asserts the **exact foreign wire** of A's `audit` task: deposits to C
     (`legio:queue:spot`), to B (`legio:queue:refine`), and back to A
     (`legio:queue:gather:audit`) — nothing else crosses the mesh;
   - asserts A's `status` completes with the transformed final result;
   - symmetric `brief` task on B: deposits to C (`spot`), to A (`utter`), back
     to B (`gather:brief`); B's `status` completes;
   - a live `fetch_peer_catalogs` round trip over the mesh proving the `input_as`
     wire parses end to end;
   - born-enabled evidence: `audit` on A is ENABLED despite branches naming
     only-remote capabilities (local-only dependency record).
2. `tests/test_leg090_catalog.py`: `input_as` appears on every served entry.
3. Phase 3 util pins: `CatalogAgentEntry` constructions carry `input_as`.

## Validation case

- This example **is** the R-9 federation validation.

## Definition of done

- All acceptance criteria met by running checks (ruff + pytest + pyright green).
- Module logging in place on every new observable event (load/resolve/deposit).
- Maintainer approval recorded (session 86e); the maintainer closes issue #49.
- Journal entry appended; the LEG-094 work tree commits when stable.