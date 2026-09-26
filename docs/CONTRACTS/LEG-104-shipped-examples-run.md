# LEG-104 — Shipped examples run exactly as documented

- **Status:** APPROVED by maintainer direction on 2026-09-26 (GitHub #52, finding 1).
- **Rasante:** R-10.x (hardening of the released `v0.1.0`)
- **GitHub issue:** #53 (finding 1 of the #52 audit umbrella)
- **Source:** external audit of `v0.1.0` (`fbd787e`), finding 1
- **Depends on:** LEG-017 (config), LEG-013 (Schema 3 tool registry), LEG-081 (CLI), LEG-100 (examples)

---

## Goal

A first-time user can copy the README quick start verbatim — from the repo
root, with no `PYTHONPATH`, no manual `mkdir` — and the shipped example node
boots, accepts a submit and returns the documented output. The suite gains the
test that would have caught finding 1.

## Problem (reproduced on a clean clone at `v0.1.0`)

- `examples/*/legio.yaml` sets relative paths (`./db/<name>.db`,
  `./patterns/<kind>`, `./tools.yaml`) that resolve against the **process
  working directory**, not the config file. `git` cannot track an empty
  `db/`, so the directory is absent from every clone.
- `examples/*/tools.yaml` declares a **repo-root** dotted path
  (`examples.tools.transform`). The installed `legio` console script does not
  put the repo root on `sys.path`, so the tool never imports without
  `PYTHONPATH`. Running from the node directory (as `CONSUMER_GUIDE.md` §5
  says) fixes the paths and breaks the import instead.
- `tests/test_leg100_consumer_guide.py` boots through `boot_node()` with a
  generated config holding **absolute** paths in a `tempfile.mkdtemp()`; it
  never loads the shipped `legio.yaml` and never goes through the `legio
  server` entry point. `scripts/validate_release.sh` passes only because it
  runs Python with the repo root as CWD.

## Scope

1. **Config-relative path resolution** (`legio.config`). A new pure helper
   `resolve_config_paths(loaded: LoadedConfig) -> LegioConfig` returns a copy in
   which relative `database.db_path`, `patterns.{tool,linguistic,composite}`
   and `tools.config` are anchored to `loaded.config_path.parent`. Absolute
   paths are returned unchanged. Applied at the node boundaries only
   (`materializer.boot_node`, the CLI catalog bootstrap and the
   `legio validate` dir resolver). `load()` keeps its pure-parse contract —
   its existing tests pin the verbatim relative paths and must keep passing.
2. **Node-local tool implementations** (`legio.tools`).
   `AvailableToolsRegistry` gains an optional `base_dir`. `load_tool()` first
   tries a normal dotted import; on `ImportError`, and only when `base_dir` is
   set, it loads `<base_dir>/<module>.py` by file location under a **unique
   synthetic module name** (no `sys.path` mutation, no cross-node name
   collision). `_build_tool_registry` passes
   `Path(config.tools.config).parent` as `base_dir` — the directory of the
   `tools.yaml`, which is the node directory.
3. **Self-provisioning database directory** (`materializer.boot_node`).
   `db_path.parent` is created (`mkdir(parents=True, exist_ok=True)`) before
   the database is opened, so a missing `db/` is not a boot failure.
4. **Self-contained examples.** Each tool-bearing node ships its own `tools.py`
   beside its `legio.yaml` and declares `implementation: "tools.<fn>"`.
   `examples/tools.py` and `examples/__init__.py` are removed.
   `examples/document_processing/tools.py` imports `pypdf` **lazily** (inside
   the tool) and the node ships `requirements.txt` (pypdf), so importing the
   example never requires the optional dependency.
5. **The missing test** (`tests/test_leg104_shipped_examples.py`).
   - A subprocess test launches the **installed `legio` console script**
     (`<venv>/bin/legio server --config examples/transform/legio.yaml
     --host 127.0.0.1 --port <free>`) from the repo root, waits for readiness,
     submits over HTTP, polls `/status`, asserts
     `{"transform": {"transformed": "HELLOHELLO"}}`, then terminates the
     process. This mirrors the audit's failing command and must fail before the
     fix (red-first).
   - A fast in-process test asserts every shipped `legio.yaml` resolves its
     relative dirs against the config file and every declared tool resolves
     through the node-local loader.
6. **Docs.** README quick start and `CONSUMER_GUIDE.md` §5 document the one
   canonical command (from the repo root) and drop the shared-module wording.

## Non-goals

- No change to `load()`'s pure-parse semantics.
- No `sys.path` mutation; no packaging change (dependency approval is LEG-107).
- No edits to the pattern YAML semantics.

## Contract changes

### `src/legio/config.py`
```python
def resolve_config_paths(loaded: LoadedConfig) -> LegioConfig:
    """Anchor relative node paths to the config file's directory."""
```
When `loaded.config_path is None`, returns `loaded.config` unchanged.

### `src/legio/tools.py`
```python
class AvailableToolsRegistry:
    def __init__(self, base_dir: Path | None = None) -> None: ...
```
`load_tool()` falls back to file-location import under a synthetic name.

### `src/legio/materializer.py`
- `boot_node`: resolve paths and create `db_path.parent`.
- `_build_tool_registry`: pass `base_dir`.

## Observability (rule 11)

- `legio.materializer` logs the resolved database path before connecting
  (`node db resolved path=...`) and, when it creates a missing directory,
  `node db dir created path=...`.
- `legio.tools` logs `tool local import name=... module=... file=...` when the
  file-location fallback is used.

## Validation

- Red-first: the subprocess test fails on the unfixed tree (`OperationalError`
  / `cannot load tool`).
- Green: the subprocess test passes; `legio validate --dry-run --config
  examples/<node>/legio.yaml` exits 0 for every deterministic shipped node.
- Full gate: `make ci` green (ruff + format-check + pyright + pytest).
