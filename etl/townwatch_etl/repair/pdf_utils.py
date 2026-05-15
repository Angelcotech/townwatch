"""
PDF slicing for repair handlers — send only the relevant page(s) to Sonnet
instead of the whole minutes document. 5-10x cheaper input + faster.

`find_motion_pages` searches the PDF text layer for the motion title;
`extract_pages` writes a new in-memory PDF with only those pages.

Falls back to the full PDF if pdfplumber can't locate the title (e.g.,
scanned PDF without text layer, or title rendered differently).
"""

from __future__ import annotations

import io
import re

import pdfplumber
import pypdf


CONTEXT_PAGES = 1   # include ±1 page around each match


def find_motion_pages(pdf_bytes: bytes, motion_title: str) -> list[int] | None:
    """
    Return 0-indexed page numbers (with ±CONTEXT_PAGES) most likely to
    contain this motion. None if the title can't be located.
    """
    if not motion_title:
        return None
    needle = _normalize(motion_title)
    if len(needle) < 8:
        return None

    matches: set[int] = set()
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            n_pages = len(pdf.pages)
            for i, page in enumerate(pdf.pages):
                try:
                    text = page.extract_text() or ""
                except Exception:
                    continue
                if not text:
                    continue
                if _fuzzy_contains(_normalize(text), needle):
                    matches.add(i)
    except Exception:
        return None

    if not matches:
        return None

    expanded: set[int] = set()
    for i in matches:
        for j in range(max(0, i - CONTEXT_PAGES), min(n_pages, i + CONTEXT_PAGES + 1)):
            expanded.add(j)
    return sorted(expanded)


def extract_pages(pdf_bytes: bytes, page_indices: list[int]) -> bytes:
    """Write a new PDF containing only the given (0-indexed) pages."""
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    writer = pypdf.PdfWriter()
    for i in page_indices:
        if 0 <= i < len(reader.pages):
            writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def trim_pdf(pdf_bytes: bytes, motion_title: str) -> tuple[bytes, str]:
    """
    High-level helper: locate the motion title and return (trimmed_pdf, note).
    Falls back to original pdf_bytes if the title can't be located.
    """
    pages = find_motion_pages(pdf_bytes, motion_title)
    if not pages:
        return pdf_bytes, "full PDF (title not located by text layer)"
    trimmed = extract_pages(pdf_bytes, pages)
    return trimmed, f"pages {pages}"


def _normalize(s: str) -> str:
    """Lowercase + collapse whitespace + strip punctuation for fuzzy matching."""
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _fuzzy_contains(haystack: str, needle: str) -> bool:
    """
    Returns True if a meaningful chunk of `needle` appears in `haystack`.
    We don't require exact match because PDF text-layer output may differ
    slightly in spacing/punctuation from how the title was extracted.
    """
    if needle in haystack:
        return True
    # Fall back to a substring of the first 40 chars of the needle
    short = needle[:40].strip()
    if short and short in haystack:
        return True
    # Fall back to the first 4 distinctive words
    words = [w for w in needle.split() if len(w) >= 4][:4]
    if len(words) >= 2 and all(w in haystack for w in words):
        return True
    return False
