"""
Escalating-filter recovery for document extraction.

A single extraction call fails in several distinct ways as documents scale —
output truncation, empty/starved responses, unreadable PDFs, schema misses.
Rather than fail a whole document on the first problem, extraction runs as an
ESCALATING FILTER: the document is split into page-windows, and each window
that doesn't resolve falls through to the next, more aggressive recovery
stage. A window only becomes a reported *anomaly* once every stage is spent.

The ladder, per window (cheapest → most aggressive, stop at first success):

  1. primary        — text-layer extract if the doc has one, else vision
  2. retry          — same call again (clears transient model variance)
  3. sub-chunk      — split the window in half and recurse; smaller windows
                      can't truncate, so this dissolves size/density failures
  4. cross-strategy — try the OTHER modality for this page range
                      (text-layer ⇄ vision)
  5. pdf-repair     — re-encode the sub-PDF and retry vision (corrupt files)
  6. classify+report— tag the reason and hand the admin a structured anomaly

Key properties:
  * Escalating filter: each stage only ever sees what the previous stage
    could NOT resolve. Windows resolved cleanly are never reprocessed.
  * DB-free: returns an ``ExtractionReport`` (stats + classified anomalies);
    the calling job persists anomalies and the run's success rate. Keeps the
    extractor pure and unit-testable.
  * Parameterised by the per-window extractor + merge callbacks, so minutes,
    agendas, and any future document extractor share one engine.

This is the document-extraction instance of a pattern meant to cover every
onboarding/maintenance stage: filter what succeeds, escalate what doesn't,
classify the irreducible residue for a human, and measure the success rate.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

import anthropic
import pypdf

from .chunking import PAGE_BREAK, PAGE_WINDOW


# --- anomaly taxonomy: the "appropriate reason for failing" --------------
# Each points at a different patch: add a recovery strategy, repair/refetch
# the source, fix a schema field, or confirm a genuine "no real record".
TRUNCATION_IRREDUCIBLE = "truncation_irreducible"   # truncates even at 1 page
MODEL_EMPTY = "model_empty_exhausted"               # empty output after retries
PDF_UNREADABLE = "pdf_unreadable"                    # bad PDF, even after repair
SCHEMA_VIOLATION = "schema_violation"               # output failed the schema
EMPTY_CONTENT = "empty_content"                      # no extractable content
UNKNOWN = "unknown_extraction_error"


def classify_failure(attempts: list[dict]) -> str:
    """Infer the anomaly class from the trail of failed attempts. Looks at the
    error types seen across the whole ladder for this window."""
    errs = " ".join(f"{a.get('error_type','')}: {a.get('error','')}" for a in attempts).lower()
    if "badrequest" in errs and ("pdf" in errs or "not valid" in errs or "base64" in errs):
        return PDF_UNREADABLE
    if "jsondecode" in errs or "delimiter" in errs or "expecting" in errs:
        return TRUNCATION_IRREDUCIBLE
    if "no json object found" in errs or "stop_reason=max_tokens" in errs:
        return MODEL_EMPTY
    if "validationerror" in errs:
        return SCHEMA_VIOLATION
    return UNKNOWN


@dataclass
class WindowAnomaly:
    start_page: int
    end_page: int
    kind: str
    attempts: list[dict]


@dataclass
class ExtractionReport:
    total_units: int = 0          # resolution units (windows, after any splits)
    clean: int = 0                # resolved on the first primary attempt
    recovered: int = 0            # resolved only after escalating
    anomalies: list[WindowAnomaly] = field(default_factory=list)
    method: str = ""              # dominant extraction modality (text_layer/vision/...)

    @property
    def resolved(self) -> int:
        return self.clean + self.recovered

    @property
    def fully_resolved(self) -> bool:
        return not self.anomalies

    def summary(self) -> str:
        kinds = ", ".join(sorted({a.kind for a in self.anomalies})) or "none"
        return (f"units={self.total_units} clean={self.clean} "
                f"recovered={self.recovered} anomalies={len(self.anomalies)} ({kinds})")


@dataclass
class Source:
    """A document as both a PDF (always) and optional per-page text layer."""
    pdf_path: Path
    pages_text: list[str] | None   # one entry per page, or None if no text layer
    total_pages: int


def build_source(pdf_path: Path, text_layer: str | None) -> Source:
    """text_layer is the page-marked joined text from pdf_text (or None)."""
    pages = text_layer.split(PAGE_BREAK) if text_layer else None
    n = len(pypdf.PdfReader(str(pdf_path)).pages)
    return Source(pdf_path=pdf_path, pages_text=pages, total_pages=max(n, 1))


def source_from_store(pdf_path: Path, pages: list[str], method: str) -> tuple[Source, str]:
    """Build a ladder Source from document_text.get_or_recover output.

    `pages`/`method` come straight from the content-addressed text store, which
    already paid for text-layer or OCR recovery once. Returns (source, read_method)
    where read_method is the honest underlying read: 'text_layer', 'ocr', or
    'vision' (the latter when the store has no usable text — stub/none/not_pdf —
    so the ladder resolves each window by vision, exactly as before the store).
    """
    text_layer = PAGE_BREAK.join(pages) if pages else None
    source = build_source(pdf_path, text_layer)
    read_method = method if method in ("text_layer", "ocr") else "vision"
    return source, read_method


def _sub_pdf(pdf_path: Path, a: int, b: int) -> Path:
    """Write pages a..b (1-indexed inclusive) to a temp PDF; caller unlinks."""
    reader = pypdf.PdfReader(str(pdf_path))
    writer = pypdf.PdfWriter()
    for i in range(a - 1, min(b, len(reader.pages))):
        writer.add_page(reader.pages[i])
    fd = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    with fd:
        writer.write(fd)
    return Path(fd.name)


def _repair_pdf(pdf_path: Path) -> Path | None:
    """Re-encode a PDF to recover from minor corruption (clears the structure
    pypdf can parse and rewrites it). Returns a new temp path or None."""
    try:
        reader = pypdf.PdfReader(str(pdf_path), strict=False)
        writer = pypdf.PdfWriter()
        for p in reader.pages:
            writer.add_page(p)
        fd = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        with fd:
            writer.write(fd)
        return Path(fd.name)
    except Exception:
        return None


def extract_with_ladder(
    source: Source,
    *,
    text_window_fn: Callable[[str], Any],
    vision_window_fn: Callable[[Path], Any],
    merge_fn: Callable[[list[tuple[Any, int]]], Any],
) -> tuple[Any, ExtractionReport]:
    """Resolve a whole document through the escalating ladder.

    text_window_fn(text)  -> Extraction for a window's text
    vision_window_fn(pdf) -> Extraction for a window's sub-PDF
    merge_fn([(extraction, page_offset), ...]) -> merged Extraction

    Returns (merged_extraction, report). Raises RuntimeError only if NOTHING
    in the document could be resolved.
    """
    report = ExtractionReport()
    resolved: list[tuple[Any, int]] = []

    def text_of(a: int, b: int) -> str | None:
        if source.pages_text is None:
            return None
        return PAGE_BREAK.join(source.pages_text[a - 1 : b])

    def run_vision(a: int, b: int, repair: bool = False) -> Any:
        sub = _sub_pdf(source.pdf_path, a, b)
        try:
            target = sub
            if repair:
                fixed = _repair_pdf(sub)
                if fixed is None:
                    raise anthropic.BadRequestError  # type: ignore[call-arg]
                target = fixed
            try:
                return vision_window_fn(target)
            finally:
                if repair and target != sub:
                    target.unlink(missing_ok=True)
        finally:
            sub.unlink(missing_ok=True)

    def primary_attempts(a: int, b: int) -> Iterator[tuple[str, Callable[[], Any]]]:
        t = text_of(a, b)
        if t is not None and t.strip():
            yield "text", (lambda: text_window_fn(t))
            yield "text_retry", (lambda: text_window_fn(t))
        else:
            yield "vision", (lambda: run_vision(a, b))
            yield "vision_retry", (lambda: run_vision(a, b))

    def alt_attempts(a: int, b: int) -> Iterator[tuple[str, Callable[[], Any]]]:
        # cross-strategy (the other modality), then PDF repair
        if source.pages_text is not None:
            yield "cross_vision", (lambda: run_vision(a, b))
        else:
            t = text_of(a, b)
            if t and t.strip():
                yield "cross_text", (lambda: text_window_fn(t))
        yield "pdf_repair", (lambda: run_vision(a, b, repair=True))

    # Phase A: primary + retry, recursively sub-chunking failures (same modality).
    worklist: list[tuple[int, int, bool]] = []   # (start, end, is_top_level)
    p = 1
    while p <= source.total_pages:
        end = min(p + PAGE_WINDOW - 1, source.total_pages)
        worklist.append((p, end, True))
        p = end + 1

    leftovers: list[tuple[int, int, list[dict]]] = []   # (a, b, attempts) for Phase B
    while worklist:
        a, b, top = worklist.pop(0)
        attempts: list[dict] = []
        done = False
        for i, (name, fn) in enumerate(primary_attempts(a, b)):
            try:
                ext = fn()
                resolved.append((ext, a - 1))
                report.total_units += 1
                if top and i == 0:
                    report.clean += 1
                else:
                    report.recovered += 1
                done = True
                break
            except Exception as e:
                attempts.append({"strategy": name, "error_type": type(e).__name__, "error": str(e)[:300]})
        if done:
            continue
        if b > a:
            # escalate finer: split in half, process the halves next (front).
            mid = a + (b - a) // 2
            worklist.insert(0, (mid + 1, b, False))
            worklist.insert(0, (a, mid, False))
        else:
            leftovers.append((a, b, attempts))

    # Phase B: alternate strategies on the irreducible (1-page) leftovers.
    for a, b, attempts in leftovers:
        done = False
        for name, fn in alt_attempts(a, b):
            try:
                ext = fn()
                resolved.append((ext, a - 1))
                report.total_units += 1
                report.recovered += 1
                done = True
                break
            except Exception as e:
                attempts.append({"strategy": name, "error_type": type(e).__name__, "error": str(e)[:300]})
        if not done:
            # Phase C: every stage spent — classify and hand to the admin.
            report.total_units += 1
            report.anomalies.append(
                WindowAnomaly(start_page=a, end_page=b, kind=classify_failure(attempts), attempts=attempts)
            )

    if not resolved:
        raise RuntimeError(
            f"extraction failed for every page; anomalies: "
            f"{[ (an.start_page, an.kind) for an in report.anomalies ]}"
        )
    return merge_fn(resolved), report
