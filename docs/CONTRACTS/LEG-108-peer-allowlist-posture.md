# LEG-108 — Peer allowlist enforced and honest default posture

- **Status:** DRAFT — awaiting maintainer approval.
- **Rasante:** R-10.x (hardening of the released `v0.1.0`)
- **GitHub issue:** #57 (findings 5/6 of the #52 audit umbrella)
- **Source:** external audit of `v0.1.0` (`fbd787e`), findings 5, 6
- **Depends on:** LEG-017 (security), LEG-027 (auth middleware), LEG-092 (work-items/deposits)

---

## Goal

The trust model the maintainer stated holds in code: **the node that receives
a user request authenticates that user, and the peer forwarding it with the
federation token is trusted**; the receiving side enforces its configured
**peer allowlist** on every federation endpoint. The default bind posture and
the documentation match the actual guarantees.

## Trust model (maintainer ruling, 2026-09-26)

- Authenticating the end user is the responsibility of the node that receives
  the user's request. The receiving node trusts a peer that presents the shared
  federation token.
- Therefore per-`task_id`-author isolation across peers is **not** required;
  what is required is that an **unknown peer is rejected in production**, not
  only in tests: "the allowlist must be used in the implementation; the tests
  validate the implementation."

## Problem

- `AuthMiddleware` (`src/legio/security/middleware.py`) already decides
  `FORBIDDEN_403` for an unknown `peer_id`, but **nothing imports it outside
  tests**. The served surface (`api.py`) checks only the shared token, so the
  peer allowlist is enforced nowhere on the receiving side, while README /
  `ARCHITECTURE.md` §10 read as though it is.
- `ApiConfig.host` defaults to `"0.0.0.0"` and, with no `api.clients`
  configured, the server reads `client_id` from the request — a self-asserted
  identity. No example sets `api:`, and the guide never says the documented
  happy path is the unauthenticated one.

## Scope

1. **Enforce the peer allowlist in the served surface** for federation
   endpoints (`/catalog`, `/work-items/{agent}`, `/outbox`, `/outbox/{id}`,
   `/deposits`). A request whose identified peer is not in the configured
   allowlist is rejected `403` (visible, rule 9). The caller identifies itself
   the way the surface already expects; a request with no identified peer is
   handled per the current contract.
2. **Wire `AuthMiddleware` (or its pure decision) into `create_app`/the request
   path** so the tests exercise the production path, not a parallel one.
3. **Default `api.host` to `127.0.0.1`.** Binding to all interfaces becomes an
   explicit consumer choice.
4. **Documentation**: state plainly that a node with no `api.clients` accepts
   any caller and trusts the `client_id` it is given (and must not be exposed),
   and align README / `ARCHITECTURE.md` §10 with the code.

## Non-goals

- Per-peer tokens; per-`task_id`-author isolation (deliberately out — see the
  trust model).
- Requiring `api.clients` (a behaviour change, a future opt-in).

## Contract changes

### `src/legio/api.py` / `src/legio/materializer.py`
The federation guard consults the configured peer allowlist and returns `403`
(`forbidden`) for an unknown peer, consistent with `AuthMiddleware`.

### `src/legio/config.py`
`ApiConfig.host` default `"127.0.0.1"`.

## Observability (rule 11)

- `api federation forbidden peer=%s path=%s` WARNING when the allowlist
  rejects; existing `api * unauthorized` warnings remain.

## Validation

Red-first tests: an unknown peer is rejected `403` on each federation endpoint
through the real app; a known peer is allowed; the default config binds
`127.0.0.1`. (`test_leg081_config.py` default assertion updated.)
