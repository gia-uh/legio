# legio examples

Five self-contained, **domain-free** example nodes (AGENTS.md rule 7: generic
operation names only — no consumer-domain vocabulary or data). Each node is a
copy-and-adapt skeleton of a real deployment:

| Example node | Flow | Kind of agent(s) | Tool |
| --- | --- | --- | --- |
| `transform/` | atomic tool step | tool | `transform` |
| `summarize/` | linguistic → tool (single branch) | linguistic + tool + composite | `assess` |
| `extract-and-summarize/` | linguistic → tool (single branch) | linguistic + tool + composite | `assess` |
| `distribute-summary/` | two independent linguistic branches | linguistic ×2 + composite | — |
| `document_processing/` | tool → linguistic (single branch) | tool + linguistic + composite | `pdf_extract_text` |

Every node ships the same skeleton:

- `patterns/{tool,linguistic,composite}/*.yaml` — Schema 1 patterns.
- `tools.yaml` — Schema 3: `available_tools` → `implementation` (dotted path,
  resolved at runtime) + `policy`.
- `tools.py` — the node-local implementation the declarations point at
  (`tools.transform`, `tools.assess`, …). It resolves beside the `tools.yaml`,
  so the node is self-contained wherever it is copied.
- `composites.py` — the node-local concrete composite classes
  (`COMPOSITE_CLASSES = {pattern_name: class}`), declared by `composites.config`
  in `legio.yaml` (LEG-110). Required only when the node ships composite
  patterns; it makes `legio server --config …` boot them with no Python
  boilerplate.
- `legio.yaml` — the LEG-017 node configuration (`node`, `database`,
  `patterns`, `tools`, `services.llm` where a linguistic step needs an LLM,
  `lifecycle`).
- `requirements.txt` — only where the example needs an extra package
  (`document_processing` → `pypdf`; example-only, not a legio dependency).

## Run an example

Follow `docs/CONSUMER_GUIDE.md`. Relative paths resolve against the node's own
`legio.yaml`, so the command runs from the repo root. The shortest path
(headless, no LLM needed):

```bash
legio server --config examples/transform/legio.yaml
```

Then submit from another shell:

```bash
curl -s -X POST http://127.0.0.1:8000/submit \
  -H 'Content-Type: application/json' \
  -d '{"client_id":"demo","agent":"transform","payload":{"text":"hello","factor":2}}'
```

Linguistic examples need a live LLM endpoint: edit `services.llm`
(`base_url`/`model`) in the node's `legio.yaml` first.

These files are **not documentation-only**: every pattern, tool declaration and
config template is loaded by the test suite (`tests/test_leg100_consumer_guide.py`,
`tests/test_leg032_example_summarize.py`, `tests/test_leg043_examples_composites.py`)
— any drift breaks the build.