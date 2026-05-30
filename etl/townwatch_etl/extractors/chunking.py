"""
Page-window chunking for large-document extraction.

Extraction is one LLM call per document, which breaks down as documents grow
— and big-city minutes/agendas are large:
  * the output JSON exceeds max_tokens and truncates into invalid JSON;
  * on the vision path, adaptive thinking starves the output budget entirely
    (empty response);
  * the input PDF exceeds Anthropic's per-request page/size limits.

This module splits a document into bounded page-windows so each window is a
small, reliable extraction; the caller maps the per-window extractor over the
windows and merges the results. A document that fits in one window is
extracted in a single call — identical to the un-chunked path — so chunking
is safe to leave on by DEFAULT for every extraction, and only "kicks in" for
documents big enough to need it.

Why page-based: every extracted item carries a 1-indexed ``source_page``.
That gives (a) a natural, overlap-free boundary — each page belongs to
exactly one window, and each item is anchored to its start page, so it's
extracted exactly once — and (b) a way for the merge step to offset-correct
window-local page numbers back to whole-document numbering.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pypdf


# Must match the separator pdf_text._extract_text_layer joins pages with.
PAGE_BREAK = "\n\n--- PAGE BREAK ---\n\n"

# Pages per window. Small enough that a window's worth of dense minutes/agenda
# JSON fits well inside a 32K output budget; large enough to keep call count
# (and cost) reasonable. Documents at or under this size are a single window.
PAGE_WINDOW = 10


def pdf_page_count(pdf_path: Path) -> int:
    return len(pypdf.PdfReader(str(pdf_path)).pages)


def split_pdf_to_windows(
    pdf_path: Path, window: int = PAGE_WINDOW
) -> list[tuple[int, Path]]:
    """Split a PDF into ``window``-page sub-PDFs written to temp files.

    Returns ``[(start_page_1indexed, sub_pdf_path), ...]``. A document with
    <= ``window`` pages yields a single ``(1, copy)`` entry. Callers MUST
    unlink the returned temp paths when done.
    """
    reader = pypdf.PdfReader(str(pdf_path))
    n = len(reader.pages)
    out: list[tuple[int, Path]] = []
    for start in range(0, max(n, 1), window):
        writer = pypdf.PdfWriter()
        for i in range(start, min(start + window, n)):
            writer.add_page(reader.pages[i])
        fd = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        with fd:
            writer.write(fd)
        out.append((start + 1, Path(fd.name)))
    return out


def split_text_to_windows(
    text: str, window: int = PAGE_WINDOW
) -> list[tuple[int, str]]:
    """Split page-marked text (joined on PAGE_BREAK) into ``window``-page
    chunks. Returns ``[(start_page_1indexed, chunk_text), ...]``; text with
    <= ``window`` pages yields a single ``(1, text)`` entry."""
    pages = text.split(PAGE_BREAK)
    out: list[tuple[int, str]] = []
    for start in range(0, len(pages), window):
        chunk = PAGE_BREAK.join(pages[start : start + window])
        out.append((start + 1, chunk))
    return out


def extend_unique(dst: list[str], src: list[str]) -> None:
    """Append items from ``src`` to ``dst`` skipping case/space-insensitive
    duplicates — for unioning attendance lists across windows in place."""
    seen = {s.strip().casefold() for s in dst}
    for item in src:
        key = (item or "").strip().casefold()
        if key and key not in seen:
            seen.add(key)
            dst.append(item)
