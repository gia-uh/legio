# Document Processing Example

A runnable legio example of a document pipeline: **PDF text extraction → LLM
summarization**, returning both the extracted text and the summary.

The composite fans out to two independent branches — one that extracts the
text and one that extracts and then summarizes it:

```yaml
branches:
  - [pdf_extract_text]                    # leaf output_as: pdf_output
  - [pdf_extract_text, summarize_text]    # leaf output_as: summary_output
```

A branch reads the composite's input, never another branch's output, so both
branches start from `pdf_extract_text` (the extraction runs twice).

## Prerequisites

- **`pypdf`** — PDF text extraction (pure Python, no system deps). It is an
  **example-only** dependency, not part of `legio`:
  ```bash
  uv pip install pypdf   # or: pip install pypdf
  ```
- **A running OpenAI-compatible LLM endpoint** for the summarizer. Point
  `services.llm` in `legio.yaml` at it (`base_url`/`model`). A local server
  (ollama, vLLM, LM Studio) needs no API key; a cloud endpoint takes
  `LEGIO_LLM_API_KEY` (secrets are environment-only).

## Run the example

From the repo root:

```bash
export LEGIO_CLIENT_TOKEN_DEMO=demo-token     # api.clients.demo token (env-only)
legio server --config examples/document_processing/legio.yaml   # default port 8000
```

Then, in another shell:

```bash
# 1. Submit (the demo client token is required)
TASK_ID=$(curl -s -X POST http://localhost:8000/submit \
  -H "Authorization: Bearer demo-token" \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "demo",
    "agent": "doc_pipeline",
    "payload": {"file_path": "fixtures/sample.pdf"}
  }' | python -c 'import json,sys; print(json.load(sys.stdin)["task_id"])')

# 2. Poll for the result (repeat until "state": "completed")
curl -s "http://localhost:8000/status/${TASK_ID}?client_id=demo" \
  -H "Authorization: Bearer demo-token"
```

## Expected output

```json
{
  "state": "completed",
  "output": {
    "pipeline_output": {
      "pdf_output": {
        "text": "Sample PDF for legio document-processing example\n…",
        "pages": 1,
        "metadata": {"Title": "Sample Document", "Author": "legio examples"}
      },
      "summary_output": {
        "summary": "This document is a sample PDF for testing legio's document processing pipeline."
      }
    }
  }
}
```

## What this demonstrates

| Component | File | Purpose |
|-----------|------|---------|
| **Tool (atomic)** | `patterns/tool/pdf_extract_text.yaml` | Real I/O: reads a PDF from disk via `pypdf` |
| **Linguistic (atomic)** | `patterns/linguistic/summarize_text.yaml` | LLM call producing the summary |
| **Composite** | `patterns/composite/doc_pipeline.yaml` | Two branches (extract; extract → summarize), output built by its class |
| **Composite class** | `composites.py` | `COMPOSITE_CLASSES` (LEG-110): merges the branch payloads |
| **Tool impl** | `tools.py` | Real `pdf_extract_text` using `pypdf` (lazy import) |
| **Config** | `legio.yaml` | Node config: patterns, tools, composites, `services.llm`, `api.clients` |
| **Fixture** | `fixtures/sample.pdf` | Real PDF with extractable text (committed) |

## Files

```
examples/document_processing/
├── README.md
├── legio.yaml
├── tools.yaml
├── tools.py
├── composites.py
├── requirements.txt
├── fixtures/
│   └── sample.pdf
└── patterns/
    ├── tool/pdf_extract_text.yaml
    ├── linguistic/summarize_text.yaml
    └── composite/doc_pipeline.yaml
```

## Tests

The example is kept honest by the suite (drift breaks the build):

```bash
# From the repo root
uv run pytest tests/test_leg100_consumer_guide.py   # config parses, patterns load, branches resolve
uv run pytest tests/test_leg110_node_local_composites.py   # the node boots its composite from config
uv run pytest tests/test_leg113_document_processing.py     # the composite's output matches its schema
```

## Extending

1. **Add a tool** in `tools.py` and declare it in `tools.yaml` with a
   `policy.timeout` (and `retries` if the call should be retried).
2. **Write its pattern** in `patterns/tool/`.
3. **Chain it** in a composite branch (`patterns/composite/`), or add a new
   branch; register any new composite class in `composites.py`.
4. **Add fixtures** under `fixtures/`.
