"""
Tiered PDF → text extraction (June pattern).

Tier 1: Pull text directly from the PDF's text layer (free, instant).
Tier 2: Run ocrmypdf locally to embed a text layer, then pull text.
Tier 3: Caller's responsibility — fall back to Claude vision on the PDF.

For Grovetown's scanned minutes, Tier 1 returns nothing and Tier 2 does
all the work. For digital-text PDFs from other jurisdictions, Tier 1
short-circuits before any subprocess is launched.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


ExtractionMethod = Literal["text_layer", "ocr", "failed"]


@dataclass
class TextExtractionResult:
    text: str | None
    method: ExtractionMethod
    pages: int = 0


def _extract_text_layer(pdf_path: Path) -> tuple[str, int, int]:
    """
    Tier 1: pdfplumber on the existing text layer.
    Returns (joined_text_with_markers, page_count, content_char_count).
    content_char_count excludes the page-break separators so we can detect
    'PDF had pages but nothing useful on them'.
    """
    import pdfplumber  # lazy import — heavy dep

    pages_text: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for p in pdf.pages:
            pages_text.append((p.extract_text() or "").strip())
        page_count = len(pdf.pages)
    content_chars = sum(len(p) for p in pages_text)
    joined = "\n\n--- PAGE BREAK ---\n\n".join(pages_text).strip()
    return joined, page_count, content_chars


def _run_ocrmypdf(input_path: Path, output_path: Path) -> None:
    """Tier 2: ocrmypdf adds a searchable text layer to a scanned PDF."""
    if shutil.which("ocrmypdf") is None:
        raise RuntimeError(
            "ocrmypdf is not installed. Install with: brew install ocrmypdf"
        )
    subprocess.run(
        [
            "ocrmypdf",
            "--skip-text",          # don't OCR pages that already have text
            "--output-type", "pdf",
            "--quiet",
            "-l", "eng",
            "--rotate-pages",       # auto-detect page rotation
            "--deskew",             # straighten scanned pages
            str(input_path),
            str(output_path),
        ],
        check=True,
        timeout=600,
        capture_output=True,
    )


CONTENT_CHAR_THRESHOLD = 200  # minimum real content (excl. page-break markers) to trust a text layer


def extract_text(pdf_path: Path) -> TextExtractionResult:
    """Attempt Tier 1, fall back to Tier 2. Never raises for normal failures."""
    page_count = 0

    # Tier 1: text layer (digital PDFs)
    try:
        text, page_count, content_chars = _extract_text_layer(pdf_path)
        if content_chars >= CONTENT_CHAR_THRESHOLD:
            return TextExtractionResult(text=text, method="text_layer", pages=page_count)
    except Exception:
        pass

    # Tier 2: ocrmypdf, then text layer (scanned PDFs)
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            ocr_path = Path(f.name)
        try:
            _run_ocrmypdf(pdf_path, ocr_path)
            text, page_count, content_chars = _extract_text_layer(ocr_path)
            if content_chars >= CONTENT_CHAR_THRESHOLD:
                return TextExtractionResult(text=text, method="ocr", pages=page_count)
        finally:
            ocr_path.unlink(missing_ok=True)
    except Exception:
        pass

    return TextExtractionResult(text=None, method="failed", pages=page_count)
