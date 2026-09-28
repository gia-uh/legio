"""LEG-113 — the document_processing example is coherent under the real model.

The composite returns the merge of its branches' leaf ``output_as`` values
(construction + re-keying, no accumulated blackboard). This test pins the
example's declared output to that merge and exercises the concrete class.
"""

from __future__ import annotations

import pytest
from beaver import AsyncBeaverDB

from legio.materializer import load_composite_classes
from legio.patterns import resolve_composite_branches
from tests.conftest import EXAMPLES, load_example_node


def test_doc_pipeline_output_keys_are_the_branch_leaves() -> None:
    catalog = load_example_node("document_processing")
    spec = catalog.specs["doc_pipeline"]
    assert spec.output.output_schema is not None
    properties = set(spec.output.output_schema["properties"])
    assert properties == {"pdf_output", "summary_output"}


@pytest.mark.asyncio
async def test_doc_pipeline_build_matches_the_declared_shape(
    beaver_db: AsyncBeaverDB,
) -> None:
    catalog = load_example_node("document_processing")
    spec = catalog.specs["doc_pipeline"]
    classes = load_composite_classes(EXAMPLES / "document_processing" / "composites.py")
    branches = resolve_composite_branches(spec, catalog)
    composite = classes["doc_pipeline"](
        agent_id="doc_pipeline",
        db=beaver_db,
        branches=branches,
        input_as=spec.input.input_as,
        output_as=spec.output.output_as,
        input_schema=spec.input.input_schema,
        output_schema=spec.output.output_schema,
    )
    branch_payloads = {
        "branch-1": {"pdf_output": {"text": "hello", "pages": 1, "metadata": {}}},
        "branch-2": {"summary_output": {"summary": "a summary"}},
    }
    result = await composite.build_output_as(branch_payloads)
    assert result == {
        "pipeline_output": {
            "pdf_output": {"text": "hello", "pages": 1, "metadata": {}},
            "summary_output": {"summary": "a summary"},
        }
    }
