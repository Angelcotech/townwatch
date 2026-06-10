"""
Agenda-packet segmentation job.

For meetings with extracted agenda items not yet segmented: get the packet,
map each item to its page range in the packet, summarize the ACTUAL proposal
document, and store it on the item. The forum/meeting then deep-links "the full
proposal · pp. X–Y" + the real summary.

The packet SOURCE is resolved per meeting:
  - a dedicated packet file (meeting.packet_url, e.g. CivicClerk "Agenda Packet"),
    if one was scraped; otherwise
  - the agenda itself — many platforms (CivicEngage/AgendaCenter, …) don't publish
    a separate packet but bundle every proposal INTO the agenda PDF, so a
    multi-page agenda IS the packet. When we detect that, packet_url is set to the
    agenda URL so the rest of the pipeline (and the UI) treat it as the packet.
  - a bare, few-page agenda has no bundled proposals: we stamp it segmented (so it
    isn't re-checked every hour) and the UI correctly shows agenda-only.

Runs in forum_tick (hourly) after extract_agendas, so a packet is segmented as
soon as its agenda items exist. Idempotent (packet_segmented_at guards re-work);
fund-gated per jurisdiction (essential — it's what makes a live forum informed).

    python -m townwatch_etl.jobs.extract_packets --all            # all unsegmented
    python -m townwatch_etl.jobs.extract_packets --all --upcoming # forum-relevant only
    python -m townwatch_etl.jobs.extract_packets --meeting-id 1636
"""

from __future__ import annotations

import argparse
import io
import sys
from typing import Any

from ..http_client import civic_get
from ..db import connect
from .. import funds
from ..extractors.packets import segment_packet
from .pipeline_errors import record_process_error as _record_process_error


# An agenda document that runs many pages IS, in practice, the meeting packet:
# platforms like CivicEngage/AgendaCenter bundle every proposal into the "agenda"
# PDF instead of publishing a separate packet file. Calibrated against the fleet —
# bare agendas run a few pages; real packets run dozens (Grovetown: 25–157). At or
# above this page count we treat the agenda AS the packet and segment it.
PACKET_MIN_PAGES = 8


def _pdf_page_count(data: bytes) -> int | None:
    """Page count if `data` is a PDF, else None (non-PDFs can't be page-segmented)."""
    if data[:5] != b"%PDF-":
        return None
    try:
        from pypdf import PdfReader
        return len(PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return None


def _stamp_segmented(meeting_id: int) -> None:
    """Mark a meeting's unsegmented items as segmented WITHOUT page ranges — used
    when the agenda is bare (no bundled proposals), so the hourly tick stops
    re-fetching it and the UI stays correctly agenda-only."""
    with connect() as conn:
        conn.execute(
            "UPDATE agenda_item SET packet_segmented_at = now() "
            "WHERE meeting_id = %s AND packet_segmented_at IS NULL",
            (meeting_id,),
        )


def _candidates(conn, *, upcoming: bool, meeting_id: int | None) -> list[dict[str, Any]]:
    # A meeting is a candidate if it has a dedicated packet OR an agenda we can
    # fall back to (a multi-page agenda doubles as the packet — see _process).
    where = ["(m.packet_url IS NOT NULL OR m.agenda_url IS NOT NULL)"]
    params: list[Any] = []
    if meeting_id is not None:
        where.append("m.id = %s")
        params.append(meeting_id)
    else:
        # has agenda items, at least one not yet segmented
        where.append(
            "EXISTS (SELECT 1 FROM agenda_item ai WHERE ai.meeting_id = m.id "
            "AND ai.data_status = 'clean' AND ai.packet_segmented_at IS NULL)"
        )
        if upcoming:
            where.append("m.meeting_date >= CURRENT_DATE")
    sql = (
        "SELECT m.id AS meeting_id, m.packet_url, m.agenda_url, gb.jurisdiction_id "
        "FROM meeting m JOIN governing_body gb ON gb.id = m.governing_body_id "
        f"WHERE {' AND '.join(where)} ORDER BY m.meeting_date"
    )
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _process(m: dict[str, Any]) -> str:
    mid = m["meeting_id"]
    jid = m["jurisdiction_id"]
    with connect() as conn:
        items = [dict(r) for r in conn.execute(
            "SELECT id, item_number, title FROM agenda_item "
            "WHERE meeting_id = %s AND data_status = 'clean' ORDER BY id", (mid,),
        ).fetchall()]
    if not items:
        return "no_items"

    # Resolve the packet source: a dedicated packet file if one was scraped, else
    # the agenda itself when it's multi-page (it bundles the proposals).
    packet_url = m.get("packet_url")
    if packet_url:
        try:
            pdf = civic_get(packet_url, timeout=90.0).content
        except Exception as e:
            print(f"  ✗ meeting {mid}: packet fetch failed: {e}")
            return "fetch_failed"
    else:
        agenda_url = m.get("agenda_url")
        if not agenda_url:
            return "no_source"
        try:
            pdf = civic_get(agenda_url, timeout=120.0).content
        except Exception as e:
            print(f"  ✗ meeting {mid}: agenda fetch failed: {e}")
            return "fetch_failed"
        pages = _pdf_page_count(pdf)
        if pages is None or pages < PACKET_MIN_PAGES:
            # Bare agenda (or non-PDF): nothing bundled to segment. Stamp so the
            # hourly tick stops re-fetching; UI stays agenda-only.
            _stamp_segmented(mid)
            print(f"  · meeting {mid}: agenda is {pages if pages is not None else 'non-PDF'} "
                  f"page(s) — no packet to segment")
            return "no_packet"
        # The agenda doubles as the packet — record it so the UI deep-links the
        # proposal pages and the pipeline treats it as the packet from here on.
        packet_url = agenda_url
        with connect() as conn:
            conn.execute(
                "UPDATE meeting SET packet_url = %s, updated_at = now() "
                "WHERE id = %s AND packet_url IS NULL",
                (packet_url, mid),
            )
        print(f"  ↳ meeting {mid}: {pages}-page agenda treated as packet")

    with funds.gate(jid, job_name="extract_packets", ref_kind="meeting",
                    ref_id=str(mid), description="packet segmentation", essential=True) as g:
        if g.paused:
            print(f"  ⏸ meeting {mid}: funds paused — deferring packet segmentation")
            return "paused"
        try:
            with connect() as conn:
                seg = segment_packet(pdf, items, conn=conn, source_url=packet_url)
        except Exception as e:
            print(f"  ✗ meeting {mid}: segmentation failed: {type(e).__name__}: {e}")
            return "seg_failed"

    # Match segments to our agenda_items by title (segmenter copies them verbatim).
    by_title = {(it["title"] or "").strip().lower(): it["id"] for it in items}
    matched = 0
    with connect() as conn:
        for s in seg.items:
            iid = by_title.get((s.title or "").strip().lower())
            if iid is None:
                continue
            conn.execute(
                "UPDATE agenda_item SET packet_start_page = %s, packet_end_page = %s, "
                "proposal_summary = %s, packet_segmented_at = now(), updated_at = now() "
                "WHERE id = %s",
                (s.start_page, s.end_page, s.summary, iid),
            )
            matched += 1
        # Stamp any unmatched items so we don't reprocess them forever.
        conn.execute(
            "UPDATE agenda_item SET packet_segmented_at = now() "
            "WHERE meeting_id = %s AND packet_segmented_at IS NULL", (mid,),
        )
    print(f"  ✓ meeting {mid}: segmented {matched}/{len(items)} item(s) from packet")
    return "ok"


def main() -> int:
    p = argparse.ArgumentParser(description="Segment agenda packets into per-item proposals")
    p.add_argument("--all", action="store_true")
    p.add_argument("--upcoming", action="store_true", help="with --all, only future meetings")
    p.add_argument("--meeting-id", type=int)
    args = p.parse_args()
    if not args.all and not args.meeting_id:
        p.error("specify --all or --meeting-id")

    with connect() as conn:
        rows = _candidates(conn, upcoming=args.upcoming, meeting_id=args.meeting_id)
    print(f"Packets to segment: {len(rows)}")
    tally: dict[str, int] = {}
    for m in rows:
        # Isolate per meeting: this runs unattended (forum_tick, --all across
        # every town in one process), so one bad packet must not abort the rest.
        try:
            out = _process(m)
        except Exception as e:
            out = "error"
            print(f"  ✗ meeting {m.get('meeting_id')}: {type(e).__name__}: {e}")
            _record_process_error("extract_packets", m.get("meeting_id"), e)
        tally[out] = tally.get(out, 0) + 1
    print(f"Done. {tally}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
