"""
Agenda-packet segmentation.

Civic platforms publish a combined "agenda packet" — one PDF holding the agenda
PLUS every supporting document (staff reports, applications, exhibits). The
agenda alone is a summary; the packet is the actual proposal a resident should
read before commenting.

This maps each of a meeting's already-extracted agenda items to the page range
in the packet where ITS materials live, and summarizes the actual document for
that item. So the forum/meeting can deep-link "the full proposal · pp. X–Y" and
show a summary of what the document really says — not just the agenda blurb.

Cheap text-layer pass (packets carry a text layer); one Haiku call segments +
summarizes the whole packet against the item titles. Metered via record_anthropic.
"""

from __future__ import annotations

import io
import json
from typing import Any

from pydantic import BaseModel, Field

from ..config import ANTHROPIC_API_KEY
from ..llm_client import record_anthropic

SEGMENT_MODEL = "claude-haiku-4-5"
_MAX_PAGE_CHARS = 2500   # per-page text budget sent to the model
# Keep one model call's packet text under this many chars (~100K tokens — well
# inside Haiku's 200K window with room for the item list, prompt, and output).
# Packets above this (major-city agendas can run many hundreds of pages) are
# split into page windows and merged. ~160 pages/window at _MAX_PAGE_CHARS.
_CHAR_BUDGET = 400_000


class ItemSegment(BaseModel):
    item_number: str | None = Field(default=None, description="The agenda item number this maps to, if given")
    title: str = Field(description="The agenda item title, copied verbatim from the provided list")
    start_page: int = Field(description="1-indexed packet page where this item's materials begin")
    end_page: int = Field(description="1-indexed packet page where this item's materials end")
    summary: str = Field(description="1-2 sentence plain-English summary of the ACTUAL document/proposal for this item")


class PacketSegmentation(BaseModel):
    items: list[ItemSegment]


def _page_texts(pdf_bytes: bytes) -> list[str]:
    from pypdf import PdfReader
    rd = PdfReader(io.BytesIO(pdf_bytes))
    out = []
    for p in rd.pages:
        t = (p.extract_text() or "").strip()
        out.append(t[:_MAX_PAGE_CHARS])
    return out


# Whole-packet prompt: the model sees every page, so it returns one entry per
# item (an item with no supporting docs points to its agenda page).
_PROMPT_WHOLE = """You are mapping a local-government agenda PACKET to its agenda items.
The packet is one PDF containing the agenda plus the supporting documents (staff
reports, applications, exhibits) for each item. Below is the packet page by page,
then the list of agenda items.

For EACH agenda item, return:
  - start_page / end_page: the 1-indexed packet page range where THAT item's
    materials live (the supporting documents for it — staff report, application,
    exhibits; if the item has no supporting docs beyond the agenda listing, point
    to the agenda page where it appears).
  - summary: 1-2 sentences on what the ACTUAL document says/proposes (the
    substance a resident needs before commenting) — not a restatement of the title.

Copy each item's title and item_number verbatim from the list. Return one entry
per agenda item, in the same order.

=== PACKET ({n} pages) ===
{packet}

=== AGENDA ITEMS ===
{items}

Respond with ONLY JSON: {{"items": [{{"item_number": "...", "title": "...", "start_page": N, "end_page": N, "summary": "..."}}]}}"""


# Window prompt: the model sees only PART of a large packet, with ABSOLUTE page
# numbers. It must return only items whose SUPPORTING DOCUMENTS actually fall in
# this window — never an item that's merely named in a one-line agenda listing
# here — so the agenda outline (which lists every item) doesn't pollute ranges.
_PROMPT_WINDOW = """You are mapping PART of a local-government agenda PACKET to its agenda items.
The full packet is one PDF (agenda plus supporting documents per item). Below is a
WINDOW of consecutive pages — page numbers in brackets are ABSOLUTE packet pages —
then the full list of agenda items.

Return an entry ONLY for items whose SUPPORTING DOCUMENTS (staff report,
application, exhibits) actually appear within THESE pages. Do NOT return an item
that is only named in a one-line agenda listing in this window but whose documents
are not here. For each returned item:
  - start_page / end_page: ABSOLUTE packet page range (inside pages {a}-{b}) where
    its materials live.
  - summary: 1-2 sentences on what the ACTUAL document says/proposes.
Copy title and item_number verbatim. If no items' documents appear here, return
an empty list.

=== PACKET PAGES {a}-{b} of {n} ===
{packet}

=== AGENDA ITEMS ===
{items}

Respond with ONLY JSON: {{"items": [{{"item_number": "...", "title": "...", "start_page": N, "end_page": N, "summary": "..."}}]}}"""


def _items_block(items: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"- item_number={it.get('item_number') or '(none)'} | title={it['title']}" for it in items
    )


def _packet_block(pages: list[str], *, start_index: int) -> str:
    """Render a run of pages with ABSOLUTE 1-indexed page labels (start_index is
    the 0-based index of the first page in the full packet)."""
    return "\n\n".join(
        f"[page {start_index + i + 1}]\n{t or '(no text on this page)'}"
        for i, t in enumerate(pages)
    )


def _call_segmenter(prompt: str) -> list[dict[str, Any]]:
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=SEGMENT_MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    record_anthropic(SEGMENT_MODEL, resp.usage)
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    s, e = text.find("{"), text.rfind("}")
    data = json.loads(text[s:e + 1]) if s >= 0 and e > s else {"items": []}
    return data.get("items", []) or []


def _clamp(raw: list[dict[str, Any]], npages: int) -> list[dict[str, Any]]:
    out = []
    for it in raw:
        try:
            sp = max(1, min(int(it.get("start_page", 1) or 1), npages))
            ep = max(sp, min(int(it.get("end_page", sp) or sp), npages))
        except (TypeError, ValueError):
            continue
        out.append({
            "item_number": it.get("item_number"),
            "title": it.get("title") or "",
            "start_page": sp, "end_page": ep,
            "summary": it.get("summary") or "",
        })
    return out


def _merge_windows(raw: list[dict[str, Any]], npages: int) -> list[dict[str, Any]]:
    """Collapse per-window segments into one entry per item. For each item:
    take the window with the LARGEST page span as the primary (so a one-line
    agenda-outline mention loses to the actual multi-page supporting docs), then
    union any window segments that are page-adjacent/overlapping with it (docs
    that straddle a window boundary). Far-away mentions are dropped."""
    by_title: dict[str, list[dict[str, Any]]] = {}
    for c in _clamp(raw, npages):
        by_title.setdefault(c["title"].strip().lower(), []).append(c)

    merged: list[dict[str, Any]] = []
    for segs in by_title.values():
        primary = max(segs, key=lambda s: s["end_page"] - s["start_page"])
        sp, ep = primary["start_page"], primary["end_page"]
        changed = True
        while changed:
            changed = False
            for s in segs:
                if s["end_page"] >= sp - 1 and s["start_page"] <= ep + 1:
                    if s["start_page"] < sp:
                        sp, changed = s["start_page"], True
                    if s["end_page"] > ep:
                        ep, changed = s["end_page"], True
        merged.append({
            "item_number": primary["item_number"], "title": primary["title"],
            "start_page": sp, "end_page": ep, "summary": primary["summary"],
        })
    return merged


def segment_packet(pdf_bytes: bytes, items: list[dict[str, Any]]) -> PacketSegmentation:
    """items: [{item_number, title}]. Returns per-item packet page ranges +
    summaries. One model call for normal packets; large packets are split into
    page windows (sized to _CHAR_BUDGET) and merged so arbitrarily long
    major-city packets segment without blowing the context window."""
    pages = _page_texts(pdf_bytes)
    npages = len(pages)
    items_block = _items_block(items)
    total_chars = sum(len(p) for p in pages) + npages * 12  # +page labels

    if total_chars <= _CHAR_BUDGET or npages <= 1:
        # Common case: the whole packet fits in one call — unchanged behavior.
        prompt = _PROMPT_WHOLE.format(
            n=npages, packet=_packet_block(pages, start_index=0), items=items_block,
        )
        return PacketSegmentation.model_validate({"items": _clamp(_call_segmenter(prompt), npages)})

    # Large packet: window it. Each window carries absolute page numbers; the
    # window prompt returns only items whose docs are actually present, and
    # _merge_windows collapses + de-noises across windows.
    per_window = max(1, _CHAR_BUDGET // _MAX_PAGE_CHARS)
    all_raw: list[dict[str, Any]] = []
    for start in range(0, npages, per_window):
        win = pages[start:start + per_window]
        prompt = _PROMPT_WINDOW.format(
            a=start + 1, b=start + len(win), n=npages,
            packet=_packet_block(win, start_index=start), items=items_block,
        )
        all_raw.extend(_call_segmenter(prompt))
    return PacketSegmentation.model_validate({"items": _merge_windows(all_raw, npages)})
