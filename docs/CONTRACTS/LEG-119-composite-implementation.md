# LEG-119 — A composite may declare its own implementation (declarative escape hatch)

- **Status**: APPROVED (maintainer direction 2026-09-28: "sí es importante la vía
  declarativa" + a test example in legio).
- **Type**: feature (Schema 1 + materializer) — GitHub issue to be opened.
- **Related**: LEG-040/§4.10.6 (composition is the agent's implementation),
  LEG-104 (node-local tools), LEG-118 (build default resolves the schema).

## Problem

A composite's construction is **the agent's implementation** (§4.10.6). The
default build (resolve the declared `output_schema` from `input_as` + branches)
covers the common case; a **particular** composition must provide its own class.
Today that is only possible **programmatically**
(`materialize_agents(..., composite_classes={name: Class})`); a node booted from
config (`legio server`) has **no declarative way** to supply it.

## Contract

- **Schema 1** gains an optional `implementation:` on a `type: composite` agent:
  a dotted path to a class (a `CompositeAgent` subclass). Absent → the engine's
  default build (no change to existing patterns).
- **Resolution**: the concrete class comes from, in order of precedence:
  1. the programmatic `composite_classes` map (tests/embedders, unchanged);
  2. the pattern's declared `implementation` (normal dotted import, else a
     **node-local** module beside the config — `composites.py`, exactly like
     Schema 3 node-local tools, LEG-104);
  3. the engine default (`CompositeAgent`).
- **Fail-fast (rule 9)**: a declared `implementation` that cannot be loaded, or
  resolves to a non-`CompositeAgent` class, refuses the boot naming the agent
  and the path — never a silent fallback.
- `implementation` on an **atomic** agent is rejected at load (atomic interior is
  `tool`/`prompt`, not a class).

## Acceptance (legio test)

- A composite that declares `implementation:` loads and materializes **its**
  class; its `build_output_as` is the one that runs (observable in the result).
- Node-local resolution: the dotted module resolves beside the node config.
- A broken `implementation` (missing path / not a CompositeAgent) fails the boot
  naming the agent.
- `implementation` on an atomic agent fails the load.
- A composite **without** `implementation` keeps the built-in default (no
  regression).

## Out of scope

- Changing the default build (LEG-118).
