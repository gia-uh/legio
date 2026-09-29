# LEG-118 — A composite's output resolves its own input_as + its branches

- **Status**: APPROVED (maintainer direction 2026-09-28: "un composite recibe lo
  que hicieron sus hijos adentro, lo procesa y lo pasa al siguiente").
  **Build behaviour (2026-09-29, released in 0.1.6):** the default build resolves
  the merged payload against the declared `output_schema` **properties** — see
  `## Contract` below. The 0.1.5 variant that emitted the payload *without* that
  resolution was withdrawn (it masked a missing branch instead of reporting it).
- **Type**: defect fix (composite default build) — GitHub issue #68.
- **Related**: LEG-040/§4.10.6 (composition is the agent's implementation),
  LEG-119 (declarative class for a particular composition), LEG-117 (load order).

## Problem

A composite built its output from its **branches' payloads only**. A composite
that also **holds** a value (its own `input_as` — e.g. a transcription it fans
out to its processors) could not place it in its output, so a pipeline could not
produce `{transcript, analysis}` without re-transcribing (or a bespoke class).

## Contract

- The **composite default build** merges the composite's **own incoming payload**
  (its `input_as`) **with its branches' payloads** (each branch's built payload
  `{output_as: value}` or an `error` result) under the composite's `output_as`.
- The merged payload is **resolved against the declared `output_schema`
  properties**: only the declared property names are emitted (undeclared keys are
  dropped), and the result is verified **superset** afterwards. A branch that
  produced nothing therefore surfaces as a **missing declared property** — an
  honest, visible contract failure (`problems=<branch>:missing`) rather than a
  silent gap (errors are never silent).
- The composite's incoming payload is passed to the build seam
  (`build_output_as(info, own=...)`); `AgentBase` keeps `_output_schema`.
- A pattern needing a different composition overrides the seam (LEG-119).

## Acceptance

- A composite fed `{transcript: {...}}` with branches producing `{summary: ...}`
  and an `output_schema` declaring `transcript` + `summary` yields
  `{<output_as>: {transcript: {...}, summary: ...}}`.
- Existing composites whose schema declares only branch keys are **unchanged**.
- A branch that produced nothing leaves its declared property absent and the
  composite's strict `required` check rejects it **visibly** (`problems=…:missing`).
- Full suite green.

## Out of scope

- Renaming/flattening across keys (a pattern that wants a specific shape uses
  `input_as`/`output_as` accordingly, or a class — LEG-119).
