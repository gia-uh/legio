# LEG-017 — Security contract

Status: **CLOSED (implementation green, maintainer approved, issue closed)**.
Applies to R-8 (runtime server) and R-9 (federation). Implemented by a single
auth middleware on the node's HTTP surface.

## 1. Principle

legio provides **coarse access control at two separated levels**, each with its
own token and blast radius: *nodes talking to nodes* (federation) and *systems
using a node* (clients). Granular authorization (users, roles, tenants, quotas)
is the application layer's responsibility, implemented on top of the node.

## 2. Secrets

- **Federation token** — one long random secret, **the same value in every node
  of the federation**. Protects node-to-node traffic. "If you know the key,
  you're in the federation."
- **Client tokens** — one long random secret **per system** registered on a
  node (`api.clients`), individually revocable. Each token identifies the
  system that holds it; other systems keep working when one is invalidated.
- Secrets are loaded from configuration (environment / secret manager), never
  from YAML committed to a repository, never logged.

## 3. Surface protected — endpoint → token map

| Endpoint | Token |
|---|---|
| `POST /work-items/{agent}`, `GET /work-items/{id}`, `GET /outbox`, `POST /outbox/{id}/ack`, `GET /catalog`, `GET /health`, `POST /deposits` | **federation** |
| `submit(starting_agent)` | **client** |
| `status(task_id)` / task results | **client** + ownership |

One middleware enforces the map; there is exactly one auth check. A token of one
level never grants the other.

## 4. Client access (systems using the node)

- A system talks to a node (usually its own) presenting its **client token**.
- **Default granularity — access to all starting agents**, unless the node
  restricts a token to a subset:
  ```yaml
  api:
    clients:
      consumer-a: { token: <t1>, agents: [flow_a, flow_b] }  # restricted
      consumer-b: { token: <t2> }                            # all, by default
  ```
- **Task ownership**: the mini-manager tags every task with the `client_id`
  derived from the presenting token; `status`/results return only tasks of that
  `client_id`. A system cannot read another system's results.
- **Revocation**: removing a token from `api.clients` (or rotating its value)
  invalidates that system immediately; the others are unaffected.

## 5. Node-to-node (peers)

- Delegation happens only between **explicitly configured peers**
  (`federation.peers` allowlist: `id` + `url`). The acceptor serves the
  federation endpoints only to callers holding the **federation token**.
- To call node B, node A sends `Authorization: Bearer <federation_token>` (the
  shared value). No per-pair secrets, no handshake, no discovery.
- Pairing is configuration: nodes join the federation by sharing the same
  federation token and listing each other in `federation.peers`.

## 6. Consumer hook

The node runs embedded in the consumer application. The consumer may wrap or
replace the auth middleware to add granular control (e.g. user login, scopes)
on top of the client token. The shipped default is the coarse two-level scheme;
a consumer that needs users/RBAC implements it at its own layer.

## 7. Out of scope (operational/application layers)

- TLS termination (reverse proxy) — required in production.
- Token rotation policy and lifecycle — operator's job (see rotation in §8).
- Rate limiting, mTLS, RBAC, audit trails — application/ops layer.

## 8. Accepted threats and rotation

- **Compromised client system** → revoke just that client token; blast radius =
  one system.
- **Compromised node** → rotate the federation token **in all nodes** (inherent
  to a shared secret; accepted, since a compromised node is a major event with
  wide access anyway).
- No confidentiality or integrity on plain-HTTP; TLS is mandatory in
  production.
- Internal state (queues, registries, beaver) is never served over the network.

## 9. Contract tests

- Each endpoint rejects requests without a token (401).
- Each endpoint accepts only its level's token (§3 map); wrong level → 401.
- Unknown client token → 401; unknown peer id → 403 (not in allowlist).
- A restricted client token can submit only its listed starting agents; default
  (no restriction) → all starting agents.
- Revoking a client token blocks it immediately; other client tokens keep
  working.
- `status`/results of a task are only readable with the token of the
  `client_id` that created it.
- Tokens are never present in logs or responses.
- Middleware is pluggable: replacing it does not change endpoint signatures.