# LEG-110 — Node-local composite classes (a composite node always boots)

- **Status**: APPROVED (maintainer direction 2026-09-28: "los composites se debe
  poder cargar siempre", "se pasa el config y desde el CLI se puede").
- **Type**: defect fix (engine) — GitHub issue to be opened by the maintainer.
- **Related**: LEG-040 (no generic composite build), LEG-081 (boot), LEG-104
  (node-local tools), LEG-013 (retries — separate slice).

## Problem

A node containing a `type: composite` pattern **cannot boot through the CLI**.
`boot_node`/`materialize_agents` accept a `composite_classes` mapping, but
`serve_node` (hence `legio server --config …`) never supplies it and there is no
configuration key, no file convention, and no loader for concrete composite
classes. Reproduced:

```
$ legio server --config examples/summarize/legio.yaml
ERROR legio.materializer no concrete composite class agent=summarize
legio error: composite 'summarize' has no concrete composite class injected; …
```

So the shipped, documented composite examples (`summarize`,
`extract-and-summarize`, `distribute-summary`, `document_processing`) cannot run
with the documented command; LEG-104 only proved `transform` (no composite).

LEG-040 deliberately specifies **no generic default build** ("the composite's
output construction is the pattern's model"). That remains. The defect is the
missing **way to provide** those classes to a config-driven boot.

## Contract

1. **Node-local composites module.** A node may ship a Python file beside its
   `legio.yaml` (conventionally `composites.py`) that exposes
   `COMPOSITE_CLASSES: dict[str, type[CompositeAgent]]` — the mapping from a
   composite pattern name to its concrete class. This is the consumer-facing
   "structure to create composites".
2. **Config pointer.** `legio.yaml` gains a `composites` section:
   ```yaml
   composites:
     config: "./composites.py"   # node-local file, loaded by file location
   ```
   The pointer is **optional**: a node with no composites omits it. Relative
   paths resolve against the config file's directory, like `tools.config`
   (LEG-104).
3. **Loader.** `boot_node` loads the classes from `composites.config` when the
   programmatic `composite_classes` argument is not given, and injects them into
   `materialize_agents`. A programmatic argument (tests, embedders) still wins.
   The file is loaded by file location under a unique synthetic module name
   (no `sys.path` mutation, no cross-node collision), mirroring node-local
   tools.
4. **Fail-fast, never silent (rule 9).**
   - The module must expose `COMPOSITE_CLASSES` as a mapping of `str -> type`
     whose values subclass `CompositeAgent`; otherwise the boot fails loudly
     naming the file.
   - A declared `composites.config` that does not exist fails loudly.
   - A composite pattern with no loaded class still fails loudly, naming the
     agent (unchanged).
5. **CLI.** `legio server --composites <path>` and `legio agent …` honor the
   config pointer; the option overrides the file (highest precedence).

## Acceptance

- `legio server --config examples/summarize/legio.yaml` boots to a served node
  (materialization completes), proven by a subprocess test hitting `/health`.
- `boot_node(loaded)` with no injected classes materializes a composite from the
  config-declared module; with an injected mapping, the injected class is used.
- A module without a valid `COMPOSITE_CLASSES` fails the boot loudly.
- The four shipped composite examples carry a `composites.py` and a
  `composites.config` entry and boot through the documented command.
- Full gate green (`ruff`, `pyright`, `pytest`).

## Out of scope

- A generic default composite build (LEG-040 keeps no default).
- `policy.retries` (separate defect slice, LEG-111).
