# LEG-115 — Node config is strict: an unknown key fails the load

- **Status**: APPROVED (maintainer direction 2026-09-28: "si se pasa una llave
  desconocida … debe dar error").
- **Type**: defect fix (engine) — rule 9 (errors are never silent).
- **GitHub issue:** #65
- **Related**: LEG-017 (config), LEG-081 (boot), LEG-013 (Schema 3 tools),
  LEG-010 (Schema 1 patterns — already strict).

## Problem

Every `legio.yaml` / `tools.yaml` model was a plain pydantic `BaseModel` with the
default `extra="ignore"`: an unknown key was dropped **silently**. Real cases
this hid:

- `services.llm.api_key: "mock-key"` — no such field (secrets are env-only);
  dropped without a word.
- `api.clients.demo.token: "demo-token"` — no such field; dropped.
- A typo (`databse:`, `db_pth:`) silently used the default.

This contradicts AGENTS.md rule 9 (errors are never silent): a misconfigured
node boots as if the offending key did not exist, giving a false sense of
configuration. Pattern Schema 1 already forbids extras; the node config and the
tools file did not.

## Contract

- Every node-configuration model (`legio.yaml`: `LegioConfig` and all nested
  models) and tools-file model (`tools.yaml`: `ToolsFileConfig`,
  `ToolDeclaration`, `ToolPolicy`) rejects unknown keys (`extra="forbid"`).
- `load()` / `load_tools_file()` wrap the resulting validation failure into the
  existing `ConfigError`, naming the offending location (pydantic's `loc`),
  loudly (rule 9).
- Valid configurations are unchanged.

## Acceptance

- A `legio.yaml` with an unknown key (e.g. a misplaced `token` under
  `api.clients.<name>`, or a top-level typo) fails `load()` with a `ConfigError`.
- A `tools.yaml` with an unknown key under a declaration fails `load_tools_file`.
- Every shipped example config still loads and boots (no example relied on a
  silently ignored key — the two that did were fixed in LEG-112/113).
- Full gate green.

## Out of scope

- A warning-only mode (the maintainer chose a hard error).
- Relaxing Schema 1 (already strict).
