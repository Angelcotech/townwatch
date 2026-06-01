"""
Mistral OCR — cheap, accurate text extraction for scanned PDFs.

This replaces frontier vision as the PRIMARY path for scanned documents.
Validated head-to-head against the Sonnet-vision path on Columbia County
minutes:
  * ~50x cheaper (~$0.05/doc vs $2-3),
  * ~25x faster (~40s vs ~30min),
  * and MORE complete — vision under-extracted old scans via its page-
    windowing (one doc: vision got 3 of 25 real motions; OCR→Haiku got all 25
    with perfect names).

Integration is deliberately minimal: this returns page-marked text in exactly
the format pdf_text produces (PAGE_BREAK-joined), so it simply provides the
"text layer" a scanned PDF lacks. The entire downstream pipeline — chunking,
the recovery ladder, Haiku text extraction — then runs unchanged, and vision
automatically demotes to the ladder's cross-strategy fallback (used only when
OCR returns nothing).

Public records, so the privacy stakes are low; paid Mistral API users are
opted out of training by default. Returns None (→ vision fallback) when no key
is configured or the OCR call fails/empties.
"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx

from ..config import MISTRAL_API_KEY
from .chunking import PAGE_BREAK

_OCR_URL = "https://api.mistral.ai/v1/ocr"
_MODEL = "mistral-ocr-latest"


def available() -> bool:
    return bool(MISTRAL_API_KEY)


def ocr_pdf(pdf_path: Path, timeout: float = 240.0) -> str | None:
    """OCR a PDF via Mistral; return page-marked text (PAGE_BREAK-joined) or
    None (no key / API error / empty result → caller falls back to vision)."""
    if not MISTRAL_API_KEY:
        return None
    try:
        b64 = base64.standard_b64encode(pdf_path.read_bytes()).decode("utf-8")
        resp = httpx.post(
            _OCR_URL,
            headers={"Authorization": f"Bearer {MISTRAL_API_KEY}"},
            json={
                "model": _MODEL,
                "document": {
                    "type": "document_url",
                    "document_url": f"data:application/pdf;base64,{b64}",
                },
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        pages = resp.json().get("pages", [])
        text = PAGE_BREAK.join((p.get("markdown") or "") for p in pages).strip()
        return text or None
    except Exception as e:  # noqa: BLE001 — any failure → vision fallback
        print(f"     ⚠ Mistral OCR failed ({type(e).__name__}: {str(e)[:80]}) — falling back to vision")
        return None
