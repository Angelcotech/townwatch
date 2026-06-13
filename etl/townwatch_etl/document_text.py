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
# having none (scanned image) and sent to OCR. Shared with the agenda/minutes
# extractors (pdf_text.CONTENT_CHAR_THRESHOLD) so the store's "is there real
# text?" decision is identical to theirs — one source of truth, no drift.
from .extractors.pdf_text import CONTENT_CHAR_THRESHOLD as _TEXT_LAYER_MIN_CHARS
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
    # Postgres cannot represent NUL in text/jsonb at all (UntranslatableCharacter,
    # not escapable) — and some PDF text layers contain stray \x00 (first seen:
    # Columbia County CivicClerk packet fileId=10978). Stripping it is lossless
    # for every legitimate consumer; without this the document can never be
    # stored and re-fails on every backfill run.
    pages = [p.replace("\x00", "") for p in pages]
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
    # Maintain the URL→content index so backfill skips byte-duplicate docs under
    # other URLs (defined below; resolved at call time, so the forward ref is fine).
    _record_url(conn, source_url, hash_)


def _text_layer_pages(data: bytes) -> list[str]:
    # pdfplumber, matching the agenda/minutes extractors' text engine — so every
    # consumer of the store reads the same (higher-fidelity) text it would have
    # extracted itself, not pypdf's weaker output. Identity resolution depends on
    # this, so the store must not silently downgrade it.
    import pdfplumber  # lazy import — heavy dep
    # Open in bounded page windows: pdfplumber/pdfminer accumulate document-level
    # state for every page touched in one open, which balloons to GBs on big
    # CivicClerk packets and OOM-kills the prod container — a silent non-zero
    # death with no failure rows (Columbia 38MB/864p packet: 2.4GB peak in one
    # open vs 356MB chunked, byte-identical output). Each window re-opens from
    # the in-memory bytes, so the only repeated cost is the (cheap) xref parse.
    _CHUNK = 50
    try:
        from pypdf import PdfReader
        n = len(PdfReader(io.BytesIO(data)).pages)
    except Exception:
        n = None
    pages: list[str] = []
    if n is None:
        # Page count unknowable (odd xref) — single open, pdfplumber's parser
        # is more forgiving; these are rarely the giant packets.
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for p in pdf.pages:
                pages.append((p.extract_text() or "").strip())
                p.flush_cache()
        return pages
    for start in range(1, n + 1, _CHUNK):
        window = list(range(start, min(start + _CHUNK, n + 1)))
        with pdfplumber.open(io.BytesIO(data), pages=window) as pdf:
            for p in pdf.pages:
                pages.append((p.extract_text() or "").strip())
                p.flush_cache()
    return pages


def _ocr_pages(data: bytes) -> list[str]:
    from .extractors.mistral_ocr import ocr_pdf, PAGE_BREAK
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(data)
        path = Path(f.name)
    text = ocr_pdf(path)
    if not text:
        return []
    return [p.strip() for p in text.split(PAGE_BREAK)]


def _record_url(conn, source_url: str | None, content_hash_: str) -> None:
    """Record that source_url resolved to this content (many URLs → one hash).
    Lets backfill_document_text skip a URL whose BYTES are already stored under
    a different URL — without it, byte-duplicate docs re-process every run."""
    if not source_url:
        return
    conn.execute(
        "INSERT INTO document_text_url (source_url, content_hash) VALUES (%s, %s) "
        "ON CONFLICT (source_url) DO UPDATE SET content_hash = EXCLUDED.content_hash",
        (source_url, content_hash_),
    )


def get_or_recover(conn, data: bytes, *, source_url: str | None = None) -> tuple[list[str], str]:
    """Return (pages, method). Hits the store if we've seen these bytes before;
    otherwise recovers via text-layer → OCR, persists, and returns. The recovered
    text is paid for at most once per document, ever."""
    h = content_hash(data)
    cached = get(conn, h)
    if cached is not None:
        # Record THIS url even on a content cache hit, so a byte-identical doc
        # under a new URL is marked seen (the queue-stuck bug, fixed 2026-06-13).
        _record_url(conn, source_url, h)
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
            from .config import MISTRAL_API_KEY
            if not MISTRAL_API_KEY:
                # This doc needs OCR but the OCR key is absent — a CONFIG problem,
                # not a property of the document. Do NOT cache: an empty 'none' here
                # is content-addressed and reused forever, so it would never re-OCR
                # even after the key is set. Return uncached so it self-heals on the
                # next run once MISTRAL_API_KEY is present. (check_environment opens a
                # pipeline issue for the missing key.)
                return [], "ocr_unavailable"
            ocr = _ocr_pages(data)
            if ocr:
                pages, method = ocr, "ocr"
            else:
                method = "none"  # OCR ran but yielded nothing — a real property; cache it
    put(conn, h, source_url=source_url, method=method, pages=pages)
    return pages, method
