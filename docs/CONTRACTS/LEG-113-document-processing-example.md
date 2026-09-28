# LEG-113 — `document_processing` example is coherent and runnable

- **Status**: APPROVED (maintainer direction 2026-09-28: "soluciona todo en
  legio").
- **Type**: defect fix (example correctness + docs).
- **Related**: LEG-040/044 (construction + re-keying), LEG-110 (composite
  loading), LEG-100/104 (examples run as documented).

## Problem

The example's `doc_pipeline` composite declared an output `{extracted, summary}`
that the model cannot produce: its single branch `[pdf_extract_text,
summarize_text]` only returns the **leaf's** payload (`summary_output`), because
the payload is construction + re-keying, never an accumulated blackboard (LEG-044
/ `flow/payload.py`). Once LEG-110 made the node bootable, a submit would fail
the composite's output contract. Its `README.md` was also stale: wrong directory
name (`document-processing`), wrong port (8080 vs the config default 8000), a
missing client token header (`api.clients.demo` requires `Bearer demo-token`),
a false "No external APIs, API keys, or network services required" claim (the
summarizer is linguistic), and an output that did not match the engine.

## Contract

- `doc_pipeline` returns both the extracted text and the summary, expressed as
  the merge of its branches' leaf `output_as` values:
  ```yaml
  branches:
    - [pdf_extract_text]                    # leaf output_as: pdf_output
    - [pdf_extract_text, summarize_text]    # leaf output_as: summary_output
  ```
  and `output_schema` declares `pdf_output` + `summary_output`. Branches are
  independent (a branch reads the composite's input, never another branch's
  output), so the summary branch re-extracts — documented in the example.
- `README.md` matches the engine and the config: correct directory and port,
  the client token header, the real LLM requirement for the linguistic step,
  the achievable output, and the tests that cover the node.
- The example's `composites.py` (LEG-110) merges the branches; its output
  satisfies the declared schema.

## Acceptance

- The example's declared output keys equal the union of its branches' leaf
  `output_as` values (`pdf_output`, `summary_output`).
- The composite's `build_output_as` over synthetic branch payloads produces the
  declared shape under `pipeline_output`.
- The node boots via config with the default lingo factory and no API key
  (LEG-112).
- Full gate green.

## Out of scope

- An end-to-end submit of `doc_pipeline` in CI (needs `pypdf`, an example-only
  dependency, and a live LLM).
