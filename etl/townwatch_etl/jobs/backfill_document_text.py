"""
One-time backfill for the content-addressed document_text store.

The store (`document_text` + `document_text.get_or_recover`) only gets populated
when an extractor runs. But the extraction cache short-circuits already-processed
documents, so historically-extracted agendas/minutes/packets never re-run and
their recovered text was never saved. This job walks every known document URL and
recovers its text once, so the whole corpus is stored and nothing re-scans later
(and the future RAG embedding layer has a complete corpus to draw from).

It is safe to run repeatedly:

  * **Idempotent** — `get_or_recover` is keyed by the bytes' sha256, so a document
    already in the store costs a single SELECT (no re-OCR, no model call).
  * **Resumable** — we skip any URL already recorded as a `source_url` in the
    store before fetching, so a re-run after an interruption only does the
    remaining work. (`--force` re-fetches anyway.)
  * **Bounded** — `--limit` caps the number of fetches per run; `--dry-run` does
    no network or writes at all.
  * **Honest about failures** — every fetch/recover failure is printed and tallied;
    a per-URL failure never aborts the run.

This is NOT a re-extraction: it only recovers and stores readable text (text layer
→ OCR). It never re-runs the Claude extractors, so it does not touch structured
records or spend on the extraction models. The only spend is Mistral OCR, and only
for scanned documents not already in the store.

Usage:
    python -m townwatch_etl.jobs.backfill_document_text --dry-run
    python -m townwatch_etl.jobs.backfill_document_text --kinds minutes --limit 50
    python -m townwatch_etl.jobs.backfill_document_text            # all kinds, all docs
"""

from __future__ import annotations

import argparse
import time
from collections import Counter

from ..db import connect
from ..http_client import civic_get
from .. import document_text


# Each kind maps to the meeting column that holds its document URL. The store is
# content-addressed, so when an agenda doubles as the packet (same bytes, same
# hash) the second kind is a free SELECT — no duplicate work.
_KIND_COLUMNS = {
    "agenda": "agenda_url",
    "minutes": "minutes_url",
    "packet": "packet_url",
}


def _candidate_urls(conn, kinds: list[str], *, fips: str | None = None) -> list[tuple[str, str]]:
    """Distinct (url, kind) across the requested kinds, in a stable order.

    When `fips` is given, restrict to one jurisdiction via the same
    meeting → governing_body → jurisdiction chain the extractors use, so a
    per-jurisdiction pipeline run only reconciles its own documents.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for kind in kinds:
        col = _KIND_COLUMNS[kind]
        if fips is None:
            rows = conn.execute(
                f"SELECT DISTINCT m.{col} AS url FROM meeting m "
                f"WHERE m.{col} IS NOT NULL ORDER BY m.{col}"
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT DISTINCT m.{col} AS url FROM meeting m "
                f"JOIN governing_body gb ON gb.id = m.governing_body_id "
                f"JOIN jurisdiction j ON j.id = gb.jurisdiction_id "
                f"WHERE m.{col} IS NOT NULL AND j.fips_code = %s "
                f"ORDER BY m.{col}",
                (fips,),
            ).fetchall()
        for r in rows:
            url = r["url"]
            if url not in seen:  # first kind to claim a URL labels it
                seen.add(url)
                out.append((url, kind))
    return out


def _already_stored(conn) -> set[str]:
    """source_urls already in the store — cheap pre-filter so re-runs skip them
    without paying a fetch. (source_url is informational; identical bytes under a
    different URL still dedupe by hash inside get_or_recover.)"""
    rows = conn.execute(
        "SELECT DISTINCT source_url FROM document_text WHERE source_url IS NOT NULL"
    ).fetchall()
    return {r["source_url"] for r in rows}


def run(kinds: list[str], *, limit: int | None, dry_run: bool, force: bool,
        jurisdiction: str | None = None, max_seconds: int | None = None) -> None:
    fips = None
    if jurisdiction:
        from ..jurisdiction import load_config, jurisdiction_fips
        fips = jurisdiction_fips(load_config(jurisdiction))
    with connect() as conn:
        candidates = _candidate_urls(conn, kinds, fips=fips)
        stored = set() if force else _already_stored(conn)

    todo = [(u, k) for (u, k) in candidates if u not in stored]
    print(
        f"kinds={','.join(kinds)}"
        + (f"  jurisdiction={jurisdiction}" if jurisdiction else "")
        + f"  candidates={len(candidates)}  "
        f"already_stored={len(candidates) - len(todo)}  to_do={len(todo)}"
        + (f"  (capped at {limit})" if limit else "")
    )
    if dry_run:
        for url, kind in todo[: limit or 20]:
            print(f"  would recover [{kind}] {url}")
        if not limit and len(todo) > 20:
            print(f"  … and {len(todo) - 20} more")
        print("dry-run: no fetches, no writes.")
        return

    if limit:
        todo = todo[:limit]

    methods: Counter[str] = Counter()
    failures = 0
    deadline = (time.monotonic() + max_seconds) if max_seconds else None
    for i, (url, kind) in enumerate(todo, 1):
        if deadline and time.monotonic() > deadline:
            # Soft time budget: stop CLEANLY with the work committed so far.
            # Every store write commits on its own, so the remainder simply
            # drains on the next run — a clean exit, not a step_failed kill.
            print(f"  ⏱ time budget ({max_seconds}s) reached after {i - 1}/{len(todo)} — "
                  f"remainder drains next run")
            break
        try:
            data = civic_get(url, timeout=120.0).content
        except Exception as e:
            failures += 1
            print(f"  ✗ [{i}/{len(todo)}] {kind} fetch failed: {type(e).__name__}: {e} — {url}")
            continue
        try:
            # One connection per document: each store write commits on its own, so
            # an interruption keeps everything recovered so far.
            with connect() as conn:
                pages, method = document_text.get_or_recover(conn, data, source_url=url)
        except Exception as e:
            failures += 1
            print(f"  ✗ [{i}/{len(todo)}] {kind} recover failed: {type(e).__name__}: {e} — {url}")
            continue
        methods[method] += 1
        chars = sum(len(p) for p in pages)
        print(f"  ✓ [{i}/{len(todo)}] {kind} {method:<10} {len(pages):>4}p {chars:>8,}c  {url}")

    summary = "  ".join(f"{m}={n}" for m, n in sorted(methods.items()))
    print(f"\ndone: recovered={sum(methods.values())}  failures={failures}  [{summary}]")


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill the document_text store from known document URLs.")
    ap.add_argument(
        "--kinds", default="agenda,minutes,packet",
        help="comma-separated subset of: agenda, minutes, packet (default: all)",
    )
    ap.add_argument("--limit", type=int, default=None, help="max documents to fetch this run")
    ap.add_argument("--jurisdiction", help="restrict to one jurisdiction slug (per-jurisdiction pipeline use)")
    ap.add_argument("--dry-run", action="store_true", help="list candidates; no network, no writes")
    ap.add_argument(
        "--force", action="store_true",
        help="re-fetch even URLs already in the store (still deduped by content hash)",
    )
    ap.add_argument(
        "--max-seconds", type=int, default=None,
        help="soft time budget: stop cleanly (exit 0) once elapsed, leaving the "
             "remainder for the next run — big packet PDFs make per-document "
             "time unpredictable, and a clean partial beats a timeout kill",
    )
    args = ap.parse_args()

    kinds = [k.strip() for k in args.kinds.split(",") if k.strip()]
    bad = [k for k in kinds if k not in _KIND_COLUMNS]
    if bad:
        ap.error(f"unknown kind(s): {', '.join(bad)} (valid: {', '.join(_KIND_COLUMNS)})")

    run(kinds, limit=args.limit, dry_run=args.dry_run, force=args.force,
        jurisdiction=args.jurisdiction, max_seconds=args.max_seconds)


if __name__ == "__main__":
    main()
