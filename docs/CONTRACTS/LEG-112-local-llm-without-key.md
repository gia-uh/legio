# LEG-112 — A configured LLM endpoint with no API key boots (local inference)

- **Status**: APPROVED (maintainer direction 2026-09-28: "soluciona todo en
  legio").
- **Type**: defect fix (engine).
- **GitHub issue:** #62
- **Related**: LEG-081 (boot / lingo factory), LEG-017 §2 (secrets are env-only),
  LEG-100 (consumer guide / examples).

## Problem

`default_lingo_factory` builds `lingo.LLM(..., api_key=api_key)` with the
environment's `LEGIO_LLM_API_KEY` (possibly `None`). The underlying OpenAI client
refuses `None`:

```
OpenAIError: Missing credentials. Please pass an `api_key` …
```

Consequence: every node with a linguistic step fails to **boot** unless a key is
set — even when `services.llm.base_url` points at a local OpenAI-compatible
server (ollama, vLLM, LM Studio) that needs no key. The shipped linguistic
examples (`summarize`, `extract-and-summarize`, `distribute-summary`,
`document_processing`) document a local endpoint; the Consumer Guide §0 says
only "an LLM endpoint". The `document_processing` config even carried an
`api_key: "mock-key"` field that the `LlmConfig` schema silently ignores — a
trap (secrets are environment-only, LEG-017 §2).

## Contract

- With `services.llm` configured and no `LEGIO_LLM_API_KEY`, the default lingo
  factory passes a **placeholder key** so the client constructs; a local server
  accepts it and a cloud endpoint answers 401 at call time, visibly (rule 9).
  A DEBUG event records that no key was configured.
- `services.llm` absent still refuses the boot loudly (unchanged).
- Secrets remain environment-only: no config field accepts a key.

## Acceptance

- `default_lingo_factory(LlmConfig(...), None)` constructs without raising.
- A node whose config declares `services.llm` boots with no `LEGIO_LLM_API_KEY`
  (proven by booting `examples/document_processing/legio.yaml` with the default
  factory).
- Full gate green.

## Out of scope

- Placeholder-key semantics for cloud providers (they answer 401, visible).
- An `api_key` config field (secrets stay env-only).
