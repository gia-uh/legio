# LEG-114 — Client tokens resolve case-insensitively (`LEGIO_CLIENT_TOKEN_<NAME>`)

- **Status**: APPROVED (maintainer direction 2026-09-28: "soluciona todo en
  legio").
- **Type**: defect fix (engine).
- **GitHub issue:** #64
- **Related**: LEG-017 §2/§4 (client tokens, env-only), LEG-108 (served surface).

## Problem

The client token lookup used the `api.clients` key verbatim against the env
secrets map:

```python
token = loaded.secrets.client_tokens.get(consumer_id)  # consumer_id = "demo"
```

`client_tokens` keys are the env-var suffix (`LEGIO_CLIENT_TOKEN_DEMO` →
`"DEMO"`), and the boot warning even names `LEGIO_CLIENT_TOKEN_DEMO`. So the
documented, operator-facing env var (uppercase) did **not** resolve for a
lowercase client id: the store stayed empty and every submit answered `401`
(loud, but unusable). Only an env var with the exact id case
(`LEGIO_CLIENT_TOKEN_demo`) worked — contrary to the convention and the warning.

## Contract

- A client's token resolves from `LEGIO_CLIENT_TOKEN_<NAME>` **case-insensitively**
  between the client id and the env-var suffix: an exact key wins, then a
  case-insensitive match. `demo` finds `LEGIO_CLIENT_TOKEN_DEMO`;
  `client-a` still finds `LEGIO_CLIENT_TOKEN_client-a`.
- A client with no resolvable token is left unregistered with the existing
  loud WARNING naming `LEGIO_CLIENT_TOKEN_<ID>` (unchanged).
- Duplicate-token detection is unchanged.

## Acceptance

- `_build_client_store` with `clients={"demo": …}` and
  `LEGIO_CLIENT_TOKEN_DEMO` registers `demo`; `resolve_consumer_id` returns it.
- A lowercase env suffix keeps working; a missing token stays unregistered.
- Full gate green.

## Out of scope

- YAML token fields (still ignored by design; secrets are env-only).
