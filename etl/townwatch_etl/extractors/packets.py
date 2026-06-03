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


_PROMPT = """You are mapping a local-government agenda PACKET to its agenda items.
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


def segment_packet(pdf_bytes: bytes, items: list[dict[str, Any]]) -> PacketSegmentation:
    """items: [{item_number, title}]. Returns per-item packet page ranges + summaries."""
    import anthropic

    pages = _page_texts(pdf_bytes)
    packet_block = "\n\n".join(f"[page {i + 1}]\n{t or '(no text on this page)'}" for i, t in enumerate(pages))
    items_block = "\n".join(
        f"- item_number={it.get('item_number') or '(none)'} | title={it['title']}" for it in items
    )
    prompt = _PROMPT.format(n=len(pages), packet=packet_block, items=items_block)

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
    # Clamp page numbers into range.
    npages = len(pages)
    for it in data.get("items", []):
        it["start_page"] = max(1, min(int(it.get("start_page", 1) or 1), npages))
        it["end_page"] = max(it["start_page"], min(int(it.get("end_page", it["start_page"]) or it["start_page"]), npages))
    return PacketSegmentation.model_validate(data)
