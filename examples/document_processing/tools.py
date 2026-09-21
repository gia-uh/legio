"""Real tool implementations for document-processing example."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pypdf import PdfReader


def pdf_extract_text(file_path: str) -> Mapping[str, Any]:
    """Extract text content from a PDF file using pypdf.

    Args:
        file_path: Path to the PDF file to extract text from.

    Returns:
        Dict with extracted text, page count, and metadata.

    Raises:
        FileNotFoundError: If the PDF file does not exist.
        ValueError: If the file is not a valid PDF.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF file not found: {file_path}")

    reader = PdfReader(str(path))
    text_parts = []
    for page in reader.pages:
        page_text = page.extract_text()
        if page_text:
            text_parts.append(page_text)

    full_text = "\n".join(text_parts).strip()

    metadata = {}
    if reader.metadata:
        for key, value in reader.metadata.items():
            clean_key = key.lstrip("/")
            metadata[clean_key] = value

    return {
        "text": full_text,
        "pages": len(reader.pages),
        "metadata": metadata,
    }
