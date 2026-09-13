# LEG-090 — Per-node catalog (roster derived from capacity, symmetric)

- **Status:** spec approved (maintainer, session 85r — federation-first reprioritization).
- **Rasante:** R-9
- **Source:** `docs/PLAN.md` (LEG-090)
- **Depends on:** LEG-085 (the Runtime public face), LEG-017 (L1 federation token), LEG-081 (node boot / `BootedNode.app`).

## Goal

Each node serves its own catalog over `GET /catalog`: the roster of agents the
node can actually execute, derived from its served pattern capacity — symmetric
(no orchestrator/provider roles). A peer reads it to learn the node's
capabilities and to author remote work (LEG-091/092).

## Problem (modern-triangle mapping)

The legacy `legio.fed` model (LEG-015) is in-memory: a `Federation` with
hand-registered `_capacities` and a module-level `_NODES` registry — no beaver,
no HTTP, no boot. In the modern architecture the node's capacity is **not** a
separate register: the node's executable capacity **is** its served pattern
catalog (`pattern_catalog.served()`, LEG-021/070 — the specs actually
materialized at boot, LEG-081). The catalog endpoint derives its roster from
that single source of truth. The L1 guard uses the already-approved
`FederationTokenStore` (LEG-017) exactly as `create_app` guards client
endpoints (inline bearer check) — the standalone `AuthMiddleware` (LEG-017)
remains a pluggable option, not wired here.

## Scope

- **In scope:** the `GET /catalog` endpoint on the node's FastAPI app,
  capacity derivations from the served pattern catalog, L1 federation-token
  guard.
- **Out of scope:** remote resolution/deposit (LEG-091/LEG-092), result readback
  (LEG-093), the multi-node example (LEG-094).

## Contract & design

- `create_app(runtime, ..., pattern_catalog=None, federation_token=None)`:
  when `federation_token` is provided, the app mounts `GET /catalog`; absent → the
  endpoint is 404 (no federation surface). `boot_node` → `BootedNode.app` feeds
  `loaded.secrets.federation_token` through automatically (LEG-017 §2; absent →
  no catalog).
- Response derived from `pattern_catalog.served()`: for each served agent, its
  name (`agent`), its advertised interface (`interface.capability` = agent name,
  `interface.schema_version` = the flow/messages `SCHEMA_VERSION` the node
  speaks), and its `kind` (`tool` | `linguistic` | `composite` from the
  `AgentSpec`).
- Guard: `Authorization: Bearer <federation token>` (L1). No token → 401;
  wrong token → 401 (LEG-017 §3). No `peer_id` semantics needed for a roster
  read (a peer list arrives with LEG-092).
- Nothing sleeps, nothing is pushed (rule 8): the catalog is a passive read of
  the served catalog; changes to the roster follow pattern invalidation
  (LEG-070) naturally on the next read.

## Interface

- `GET /catalog` → `200 {schema_version: <SCHEMA_VERSION>, agents:
  [{agent: str, interface: {capability: str, schema_version: int},
  kind: str}]}`; `401 {code: "unauthorized"}` without a valid token;
  `404` when the node has no federation configured.

## Acceptance criteria

From `docs/PLAN.md` (LEG-090), verbatim:
- Catalog served over GET /catalog lists agents, interfaces and
  `schema_version`; requester token (federation) validated.

## Tests (red-first)

- `GET /catalog` lists every served agent with correct `interface` and `kind`
  (tool/linguistic/composite), top-level `schema_version` matches flow
  `SCHEMA_VERSION`.
- No token / wrong token → 401; no federation config → 404.
- Invalidated (LEG-070) patterns are absent — capacity tracks `served()`.
- Booted node with `LEGIO_FEDERATION_TOKEN` set serves the catalog over
  `BootedNode.app`.
- Footprint: the catalog adds HTTP surface only; the runtime footprints
  (`gates` + `node_ops`, Manager/Registry scopes) are untouched.

## Validation case

- LEG-094 multi-node example (catalog discovery) — later in R-9.

## Definition of done

- All acceptance criteria met by running checks (ruff + pytest + pyright green).
- Maintainer approval recorded; the maintainer closes the GitHub issue.
- Journal entry appended; the LEG-090 work tree commits when stable.