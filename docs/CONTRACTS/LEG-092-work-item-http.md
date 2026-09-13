# LEG-092 — Work-item over HTTP + remote deposit (federation-token auth)

- **Status:** spec approved (maintainer, session 85q — federation-first reprioritization).
- **Rasante:** R-9
- **GitHub issue:** #44
- **Source:** `docs/PLAN.md` (LEG-092)
- **Depends on:** LEG-090 (the acceptor's served capacity), LEG-091 (the author
  already resolved the step), LEG-017 (L1 token).

## Goal

Remote deposit: `POST /work-items/{agent}` with the shared federation token
(L1) and a matching interface deposits into the acceptor's queue. The author
has already resolved the step against this acceptor's roster (LEG-091); the
acceptor double-checks the interface and deposits.

## Problem (modern-triangle mapping)

The DRAFT referenced LEG-015 (in-memory `Federation._queues`). In the modern
architecture the acceptor's deposit **is** its own business task path: a
work-item becomes a genuine Manager seed task on the acceptor (the same
"submit → seed deposits the root ExecutionRequestMessage on the class queue" of
§7.1), **keyed by the author's task id** — `Manager.submit_task(task_id=...)`
already owns an explicit-id path and rejects a live duplicate visibly, which is
exactly the "same id executed once" idempotency LEG-093 needs.

The L1 guard uses the approved `FederationTokenStore` inline bearer check, the
same as `GET /catalog` (LEG-090). Lifecycle stays local (maintainer session
85q): work items only deposit *work*; there is no lifecycle verb over this
surface.

## Scope

- **In scope:** the `POST /work-items/{agent}` endpoint on the acceptor's app,
  federation-token auth, interface/schema match validation, the deposit seam
  `Runtime.submit_work_item` (author-minted id — idempotent, never executed
  twice), and the receipt.
- **Out of scope:** result readback/ack (the author outbox, LEG-093); the
  author-side resolver (LEG-091); the author-side HTTP client (the acceptance
  is the endpoint; the authoring client is exercised by LEG-094).

## Contract & design

- `create_app(..., federation_token=...)`: when a federation token is provided
  the app also mounts `POST /work-items/{agent}` alongside `GET /catalog`;
  absent → 404 (the same "no federation surface" rule).
- Request body `{task_id, payload, schema_version}`; `task_id` is the author's
  own `<node_id>:<uuid>` (the donor of the result key and of idempotency).
- Validation order in the endpoint:
  1. bearer federation token valid → else **401** `{code: unauthorized}`;
  2. `pattern_catalog` present → else **503** `{code: no_capacity}` (a
     federated node must never silently serve an empty roster, rule 9);
  3. `{agent}` known **and served** by the acceptor's catalog (LEG-090
     capacity) → else **404** `{code: unknown_agent}`;
  4. `schema_version == SCHEMA_VERSION` (flow/messages) → else **409**
     `{code: interface_mismatch}`;
  5. gate open on the target class (entry gate, §12.5) → else **409**
     `{code: class_disabled}`;
  6. deposit.
- Deposit seam `Runtime.submit_work_item(author, route, payload, *,
  task_id) -> WorkItemReceipt`: mirrors the business submit, but the task_id is
  the **author-provided** id and the owner is the author's node (order —
  gate, validate id, mint the flow token, `manager.submit_task(SEED_TASK,
  task_id=...)`). A duplicate `task_id` is **not an error**: the Manager's
  existing "already exists" rejection is translated into a deduplicated receipt
  (`deposited=False, deduplicated=True`) — the work is never run twice.
- Route for the work item: a served agent (atomic or composite) is addressed by
  its own `(class, input_as)` — delegation targets *capability* agents, not
  entry points (ARCH §9); no `main` requirement.
- The author's node id (the owner) is the task_id origin (`<node_id>` part),
  truthful and domain-free (rule 7).

## Interface

- `POST /work-items/{agent}` body `{task_id, payload, schema_version}` →
  `200 {id, deposited: true, deduplicated: false}` on a fresh deposit;
  `200 {id, deposited: false, deduplicated: true}` when `task_id` is already
  known (never executed twice); `401`/`404`/`409`/`503` per the validation
  order above (LEG-016 stable `code`).

## Acceptance criteria

From `docs/PLAN.md` (LEG-092), verbatim:
- POST /work-items/{agent} with valid federation token and matching interface
  deposits into the acceptor queue; mismatch → 4xx; no token → 401.

## Tests (red-first)

- The endpoint deposits a work item that reaches the acceptor's class queue as
  a root `ExecutionRequestMessage` (task id = author's id), verified over the
  booted node.
- No token / wrong token → 401; no federation configured → 404.
- Unknown / not-served agent → 404; `schema_version` mismatch → 409.
- Duplicate `task_id` → deduplicated receipt and only one execution message on
  the queue (idempotency, LEG-093 prerequisite).
- Lifecycle stays local: the surface never accepts an operator verb.

## Validation case

- LEG-094 multi-node example (author delegates to the acceptor over this
  endpoint; result readback via LEG-093).

## Definition of done

- All acceptance criteria met by running checks (ruff + pytest + pyright green).
- Maintainer approval recorded; the maintainer closes the GitHub issue.
- Journal entry appended; the LEG-092 work tree commits when stable.