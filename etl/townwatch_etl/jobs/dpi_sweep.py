"""
DPI sweep — find the vision-extraction resolution knee.

Extracts the same document window at several DPIs (plus the raw-PDF baseline)
and reports, per DPI: payload size, latency, item count, and how well the
extracted items match the raw-PDF reference. The knee is the lowest DPI that
still matches the reference — below it accuracy drops, above it only payload
and latency rise. Use the result to set config.VISION_RENDER_DPI.

Makes real vision calls, so don't run it concurrently with a big extraction
drain (shared throttle + spend). Bounded by --max-pages.

Run (after a drain finishes):
    python -m townwatch_etl.jobs.dpi_sweep --meeting-id 1503
    python -m townwatch_etl.jobs.dpi_sweep --meeting-id 1503 --kind minutes --max-pages 4
    python -m townwatch_etl.jobs.dpi_sweep --meeting-id 174 --kind agendas --dpis 300,150,100
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

from ..db import connect
from ..extractors import agendas as A
from ..extractors import minutes as M
from ..extractors.rasterize import vision_content
from ..extractors.recovery import _sub_pdf
from ..http_client import civic_get

DEFAULT_DPIS = [None, 300, 200, 150, 100]


def _payload_bytes(content: list[dict]) -> int:
    """Total base64 payload across image/document blocks."""
    return sum(len(b["source"]["data"]) for b in content if b.get("type") in ("image", "document"))


def _titles(extraction) -> list[str]:
    return sorted((it.title or "").strip().casefold() for it in extraction.agenda_items if (it.title or "").strip())


def _recall(reference: list[str], got: list[str]) -> float:
    if not reference:
        return 1.0 if not got else 0.0
    g = set(got)
    return sum(1 for t in reference if t in g) / len(reference)


def sweep(meeting_id: int, kind: str, dpis: list, max_pages: int) -> int:
    ext_mod = M if kind == "minutes" else A
    url_col = "minutes_url" if kind == "minutes" else "agenda_url"
    instructions = ext_mod.VISION_INSTRUCTIONS
    extract_window = ext_mod._extract_vision_window

    with connect() as conn:
        row = conn.execute(f"SELECT {url_col} AS url FROM meeting WHERE id=%s", (meeting_id,)).fetchone()
    if not row or not row["url"]:
        print(f"meeting {meeting_id} has no {url_col}")
        return 1

    print(f"downloading {kind} doc for meeting {meeting_id} ...")
    r = civic_get(row["url"], timeout=120.0)
    r.raise_for_status()
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(r.content)
        full = Path(f.name)
    sub = _sub_pdf(full, 1, max_pages)  # first max_pages as a representative window
    print(f"  doc={len(r.content):,}B → sweeping first {max_pages} page(s) at DPIs {dpis}\n")

    rows = []
    reference: list[str] | None = None
    for dpi in dpis:
        payload = _payload_bytes(vision_content(sub, instructions, dpi=dpi))
        t0 = time.time()
        try:
            ext = extract_window(sub, dpi=dpi)
            latency = time.time() - t0
            titles = _titles(ext)
            err = None
        except Exception as e:  # noqa: BLE001
            latency = time.time() - t0
            titles, err = [], f"{type(e).__name__}: {str(e)[:60]}"
        if dpi is None:
            reference = titles
        rows.append({"dpi": dpi, "payload": payload, "latency": latency,
                     "items": len(titles), "recall": _recall(reference or [], titles), "err": err})

    print(f"{'DPI':>6} {'payload':>10} {'latency':>9} {'items':>6} {'recall*':>8}  notes")
    print("-" * 60)
    for x in rows:
        d = "raw" if x["dpi"] is None else str(x["dpi"])
        note = x["err"] or ("← reference" if x["dpi"] is None else "")
        print(f"{d:>6} {x['payload']/1024:>8.0f}KB {x['latency']:>8.1f}s {x['items']:>6} {x['recall']*100:>7.0f}%  {note}")
    print("\n* recall = fraction of raw-PDF item titles recovered at this DPI.")
    print("  Knee = lowest DPI holding recall ~1.0 with the smallest payload/latency.")
    print(f"  Set it via VISION_RENDER_DPI once you pick one.")

    sub.unlink(missing_ok=True)
    full.unlink(missing_ok=True)
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--meeting-id", type=int, required=True)
    p.add_argument("--kind", choices=("minutes", "agendas"), default="minutes")
    p.add_argument("--max-pages", type=int, default=4, help="window size to sweep (keep small)")
    p.add_argument("--dpis", help="comma list, e.g. 300,200,150,100 (raw baseline always included)")
    args = p.parse_args()
    if args.dpis:
        dpis = [None] + [int(x) for x in args.dpis.split(",") if x.strip()]
    else:
        dpis = DEFAULT_DPIS
    return sweep(args.meeting_id, args.kind, dpis, args.max_pages)


if __name__ == "__main__":
    sys.exit(main())
