# LEG-121 — A linguistic step is sent as `system` + `user` turns

- **Status**: APPROVED (maintainer direction 2026-09-29).
- **Type**: defect fix (linguistic call shape) + Schema 1 / config field.
- **Related**: LEG-030 (linguistic agent via lingo), LEG-010 (Schema 1), LEG-031
  (prompt variable coherence), LEG-072 (`output_model` compilation).

## Problem

`LinguisticAgent` sends `lingo.create(output_model, [Message.system(prompt)])`:
**one** `system` turn carrying both the instructions and the resolved content.
Several chat templates (verified: `qwen/qwen3.8-27b` on Groq) **require a `user`
turn** and fail consistently (`No user query found in messages`); measured
0/5 with `system`-only versus 3/5 with `system`+`user`. LM Studio tolerated the
`system`-only shape, so the defect stayed latent.

The pattern also mixes the agent's **role** (who it is) with the **task**
(what to do with the data) in one string, which is not how a chat call is
modelled: the role belongs in the `system` turn, the task in the `user` turn.

## Contract

A linguistic step is a **chat call with two turns**, and the role is declarable:

- **`system` turn** — the agent's role. Taken, in precedence order, from:
  1. the pattern's `system_prompt` (a linguistic pattern may declare it), else
  2. the node config's general `system_prompt`, else
  3. empty (legio invents nothing; if nothing is declared the turn is empty).
- **`user` turn** — the resolved `prompt` (the task + its data), as today.

Both prompts keep their existing semantics:
- the pattern's `prompt` is a template resolved against the payload
  (dotted paths + system vars), variable-checked against `input_schema`
  (LEG-031);
- the pattern's `system_prompt` is **also** a template resolved against the
  payload, and its variables are checked against `input_schema` too;
- the node config's general `system_prompt` is **static text** (no variables —
  it is not bound to any pattern's `input_schema`).

`prompt` never duplicates into the `system` turn.

### Config: the general default

`legio.yaml` gains a top-level, general field:

```yaml
system_prompt: "You are a helpful assistant."
```

It is optional. Absent → no general default. It is a plain string (no
variables).

### Schema 1: the per-agent override

A `kind: linguistic` pattern may declare `system_prompt:` (a template string).
It is optional and overrides the general default for that pattern.

## Acceptance

- A linguistic step sends exactly two messages: a `system` turn and a `user`
  turn; the `user` turn carries the resolved `prompt`.
- The `system` turn is the pattern's `system_prompt` when declared, else the
  node config's general `system_prompt`, else empty.
- The pattern's `system_prompt` is resolved against the payload; a `{var}` in it
  not declared in `input_schema` is a load error (LEG-031).
- The general `system_prompt` is static (not resolved, no variables).
- `prompt` is never copied into the `system` turn.
- Full suite green; verified end-to-end on Groq (`blog_post_draft` completes).

## Out of scope

- Changing the composite or tool call shapes.
- Naming/splitting the `prompt` itself into instructions/input (the role lives
  in `system_prompt`; the task stays in `prompt`).
