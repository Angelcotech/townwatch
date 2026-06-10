"""
Content-addressed readable-text store — "convert once, reuse everywhere".

A document's recovered text (digital text layer, or Mistral OCR for scans) is
expensive to produce and was previously thrown away after feeding the model.
This module recovers it ONCE per document (keyed by the bytes' sha256) and
persists per-page text, so every extractor — agendas, minutes, packets, budgets,
and future consumers like RAG embeddings — reads it for free.

Usage:
    pages, method = document_text.get_or_recover(conn, pdf_bytes, source_url=url)
    # pages: list[str] per page; method: 'text_layer' | 'ocr' | 'none' | 'not_pdf'
"""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
from pathlib import Path

# A document with fewer than this many characters of text layer is treated as
# having none (scanned image) and sent to OCR.
_TEXT_LAYER_MIN_CHARS = 50
# A textless PDF at/under this size is a placeholder STUB (CivicEngage serves
# tiny blank PDFs for never-uploaded documents) — there's nothing to OCR, so we
# don't waste a Mistral page on it. Matches the agenda extractor's stub cutoff.
_STUB_PDF_SIZE_BYTES = 5_000


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def get(conn, hash_: str) -> dict | None:
    row = conn.execute(
        "SELECT pages, method, page_count, source_url FROM document_text WHERE content_hash = %s",
        (hash_,),
    ).fetchone()
    return row  # connect() uses a dict row factory


def put(conn, hash_: str, *, source_url: str | None, method: str, pages: list[str]) -> None:
    conn.execute(
        """
        INSERT INTO document_text (content_hash, source_url, method, page_count, pages, char_count)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s)
        ON CONFLICT (content_hash) DO UPDATE SET
            source_url = EXCLUDED.source_url, method = EXCLUDED.method,
            page_count = EXCLUDED.page_count, pages = EXCLUDED.pages,
            char_count = EXCLUDED.char_count
        """,
        (hash_, source_url, method, len(pages), json.dumps(pages), sum(len(p) for p in pages)),
    )


def _text_layer_pages(data: bytes) -> list[str]:
    from pypdf import PdfReader
    rd = PdfReader(io.BytesIO(data))
    return [((p.extract_text() or "").strip()) for p in rd.pages]


def _ocr_pages(data: bytes) -> list[str]:
    from .extractors.mistral_ocr import ocr_pdf, PAGE_BREAK
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(data)
        path = Path(f.name)
    text = ocr_pdf(path)
    if not text:
        return []
    return [p.strip() for p in text.split(PAGE_BREAK)]


def get_or_recover(conn, data: bytes, *, source_url: str | None = None) -> tuple[list[str], str]:
    """Return (pages, method). Hits the store if we've seen these bytes before;
    otherwise recovers via text-layer → OCR, persists, and returns. The recovered
    text is paid for at most once per document, ever."""
    h = content_hash(data)
    cached = get(conn, h)
    if cached is not None:
        return list(cached["pages"]), cached["method"]

    if data[:5] != b"%PDF-":
        put(conn, h, source_url=source_url, method="not_pdf", pages=[])
        return [], "not_pdf"

    try:
        pages = _text_layer_pages(data)
    except Exception:
        pages = []
    method = "text_layer"
    if sum(len(p) for p in pages) < _TEXT_LAYER_MIN_CHARS:
        if len(data) <= _STUB_PDF_SIZE_BYTES:
            pages, method = [], "stub"  # blank placeholder — nothing to OCR
        else:
            ocr = _ocr_pages(data)
            if ocr:
                pages, method = ocr, "ocr"
            else:
                method = "none"  # neither text layer nor OCR yielded text
    put(conn, h, source_url=source_url, method=method, pages=pages)
    return pages, method
