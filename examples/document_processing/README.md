# Document Processing Example

A complete, runnable legio example demonstrating a document processing pipeline:
**PDF text extraction → LLM summarization**.

## Quick Start

### Prerequisites

Install the required Python packages:

```bash
# From the repo root (legio/)
uv sync --extra dev
# Or with pip:
pip install pypdf
```

The example uses:
- **pypdf** — PDF text extraction (pure Python, no system deps)
- **legio** — the framework itself (installed via `uv sync` from repo root)

No external APIs, API keys, or network services required.

### Run the Example

```bash
# From the repo root (legio/)
cd examples/document-processing

# 1. Start the node server
legio server

# 2. In another terminal, submit a task
curl -X POST http://localhost:8080/submit \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "demo",
    "agent": "doc_pipeline",
    "payload": {"file_path": "fixtures/sample.pdf"}
  }'

# 3. Poll for the result (use the task_id from submit response)
curl -X GET "http://localhost:8080/status/<TASK_ID>?client_id=demo"
```

### Expected Output

```json
{
  "state": "completed",
  "output": {
    "pipeline_output": {
      "extracted": {
        "text": "Sample PDF for legio document-processing example\nThis PDF contains extractable text for testing.\nLine 3: Numbers 12345 and symbols !@#$%^&*()\nLine 4: Unicode: café, naïve, résumé\n\nEnd of sample document.",
        "pages": 1,
        "metadata": {
          "Title": "Sample Document",
          "Author": "legio examples",
          "Subject": "document-processing example fixture",
          "Producer": "pypdf"
        }
      },
      "summary": {
        "summary": "This document is a sample PDF for testing legio's document processing pipeline. It contains multiple lines of text including numbers, symbols, and Unicode characters to verify extraction works correctly."
      }
    }
  }
}
```

## What This Demonstrates

| Component | File | Purpose |
|-----------|------|---------|
| **Tool (atomic)** | `patterns/tool/pdf_extract_text.yaml` | Real I/O: reads PDF from disk, extracts text via pypdf |
| **Linguistic (atomic)** | `patterns/linguistic/summarize_text.yaml` | LLM call (uses MockLLM in tests) |
| **Composite** | `patterns/composite/doc_pipeline.yaml` | Chains extraction → summarization |
| **Tool impl** | `tools.py` | Real `pdf_extract_text` using pypdf |
| **Config** | `legio.yaml` | Node config with all three pattern dirs |
| **Fixture** | `fixtures/sample.pdf` | Real PDF with extractable text (committed) |

## Files

```
document-processing/
├── README.md                    # This file
├── legio.yaml                   # Node configuration
├── tools.yaml                   # Tool declarations (Schema 3)
├── tools.py                     # Real tool implementations
├── fixtures/
│   └── sample.pdf               # Test PDF (1 page, extractable text)
└── patterns/
    ├── tool/
    │   └── pdf_extract_text.yaml    # Tool pattern (Schema 1)
    ├── linguistic/
    │   └── summarize_text.yaml      # Linguistic pattern (Schema 1)
    └── composite/
        └── doc_pipeline.yaml        # Composite pattern (Schema 1)
```

## Running Tests

The example is validated by the consumer guide test:

```bash
# From repo root
uv run pytest tests/test_leg100_consumer_guide.py::test_guide_transform_node_boots_and_serves_submit_status -v
# Note: this test uses the 'transform' example; the document-processing example
# is validated by the same mechanisms (pattern loading, config parsing, etc.)
```

## Extending

To add your own document processing steps:

1. **Add a new tool** in `tools.py` (real I/O, libs, validation)
2. **Declare it** in `tools.yaml` with `policy.timeout`
3. **Write its pattern** in `patterns/tool/your_tool.yaml` (Schema 1)
4. **Chain it** in a composite (`patterns/composite/`)
5. **Add test fixtures** in `fixtures/`

All tools run locally, deterministically, with no external dependencies.