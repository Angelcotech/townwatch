"""
CivicClerk meetings scraper.

CivicClerk is a CivicPlus-owned platform commonly used by county
governments (parallel to CivicEngage / AgendaCenter, which most CITY
governments use). Examples: Columbia County GA, many other GA counties.

Unlike CivicEngage which we have to HTML-scrape, CivicClerk exposes a
clean OData JSON API at `https://{tenant}.api.civicclerk.com/v1/`.
Three endpoints we use:

  EventCategories                          → body list (with category IDs)
  Events?$filter=eventCategoryId eq N      → meetings per body
  Meetings/GetAttachmentFile(fileId=K)     → agenda/minutes PDF download

Output MeetingRecord shape mirrors `civicengage_agendacenter.MeetingRecord`
so meetings_inventory can dispatch by `platform_hints.agenda_platform`
and write the same row shape regardless of source.

Run standalone:
    python -m townwatch_etl.scrapers.civicclerk_meetings columbia-county-ga
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Iterator

from ..http_client import civic_get


# Per-host throttling, Retry-After honoring, and backoff live in
# http_client — civic_get() spaces same-host requests, so this scraper no
# longer sleeps between pages/categories of its own accord.
# CivicClerk hard-caps OData $top at 200 regardless of what we request.
# Pagination is server-driven via @odata.nextLink. Setting PAGE_SIZE >200
# does NOT increase the page; the server silently clamps and the next
# 200 require a follow-up request.
PAGE_SIZE = 200

# CivicClerk publishedFiles use a numeric `type` field. Type strings on
# the API mirror this:
#   "Agenda"        → fileType 1
#   "Agenda Packet" → fileType 2
#   "Minutes"       → fileType 4
# We always prefer the "Agenda" item over "Agenda Packet" for the
# agenda_url because the packet is the deck of supporting docs and is
# usually much larger / harder to extract.
AGENDA_TYPE_PREFERENCE = ("Agenda",)
AGENDA_PACKET_TYPE = "Agenda Packet"
MINUTES_TYPE = "Minutes"

# CivicClerk also bulk-loads historical events; their publishedAgendaTimeStamp
# carries a human-readable string like "Agenda Posted on May 4, 2019 10:17 PM".
# Same parser shape as the CivicEngage scraper but a different prefix.
POSTED_RE = re.compile(
    r"Posted\s+on\s+([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})\s+(\d{1,2}):(\d{2})\s*([AP]M)",
    re.IGNORECASE,
)

_MONTH_LOOKUP = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# CivicClerk meetingTypeName values vary. Fall back to substring detection on
# the eventName.
TYPE_PATTERNS = [
    (re.compile(r"\bwork session\b", re.I),         "workshop"),
    (re.compile(r"\bspecial(\s+called)?\b", re.I),  "special"),
    (re.compile(r"\bemergency\b", re.I),            "emergency"),
    (re.compile(r"\bexecutive session\b", re.I),    "executive_session"),
]
DEFAULT_TYPE = "regular"


@dataclass
class MeetingRecord:
    agenda_id: int                  # eventId from CivicClerk
    meeting_date: date
    meeting_type: str
    category_id: int
    category_name: str
    description: str | None
    agenda_url: str | None
    minutes_url: str | None
    packet_url: str | None = None      # combined supporting-docs deck
    agenda_posted_at: datetime | None = None
    meeting_time: time | None = None   # scheduled start (local wall-clock)


def _api_base(tenant: str) -> str:
    return f"https://{tenant}.api.civicclerk.com/v1"


def fetch_categories(tenant: str) -> list[dict]:
    """All publicly visible EventCategories for a CivicClerk tenant.
    Returns [{id, categoryDesc, sortOrder, isPublic, parentId}, ...]"""
    url = f"{_api_base(tenant)}/EventCategories"
    r = civic_get(url)
    r.raise_for_status()
    data = r.json()
    return [c for c in data.get("value", []) if c.get("isPublic")]


def fetch_events(tenant: str, category_id: int, since: "date | None" = None) -> Iterator[dict]:
    """All Events for a given category.

    CivicClerk's OData server (a) caps @odata.nextLink chains
    undocumentedly at ~200 records and (b) caps RESPONSE SIZE at ~15
    records regardless of $top. Manual $skip pagination works past
    both. We page until we get an empty response; the server's own
    page-size choice doesn't matter to us.

    `since` (a date) restricts to events on/after that day via a server-side
    eventDate filter — the incremental path, so daily runs don't re-page the
    full history. None = full sweep.
    """
    base = _api_base(tenant)
    skip = 0
    date_filter = ""
    if since is not None:
        # OData Edm.DateTimeOffset literal; '+' is the URL space the rest of
        # this query already uses.
        date_filter = f"+and+eventDate+ge+{since.isoformat()}T00:00:00Z"
    while True:
        url = (
            f"{base}/Events"
            f"?$filter=categoryId+eq+{category_id}{date_filter}"
            f"&$orderby=eventDate+asc"
            f"&$top={PAGE_SIZE}"
            f"&$skip={skip}"
        )
        r = civic_get(url)
        r.raise_for_status()
        data = r.json()
        batch = data.get("value", [])
        if not batch:
            return
        for event in batch:
            yield event
        skip += len(batch)


def _parse_posted_at(s: str | None) -> datetime | None:
    if not s:
        return None
    m = POSTED_RE.search(s)
    if not m:
        return None
    month_str, day, year, hour, minute, ampm = m.groups()
    month = _MONTH_LOOKUP.get(month_str.lower())
    if month is None:
        return None
    hour_24 = int(hour) % 12
    if ampm.upper() == "PM":
        hour_24 += 12
    try:
        return datetime(int(year), month, int(day), hour_24, int(minute))
    except ValueError:
        return None


def _attachment_url(tenant: str, file_id: int) -> str:
    # Use GetMeetingFileStream for all publishedFile types we capture
    # (Agenda, Agenda Packet, Minutes — fileTypes 1/2/4). GetAttachmentFile
    # is only valid for fileType 3 (attachments), which we don't currently
    # scrape. The previous URL pattern (GetAttachmentFile) silently 404'd
    # on every recent file because CivicClerk routes Agenda/Packet/Minutes
    # only through GetMeetingFileStream — confirmed against the SPA's own
    # URL builder. Older fileIds may have worked via GetAttachmentFile
    # because of historical aliasing; GetMeetingFileStream works for all.
    return (
        f"{_api_base(tenant)}/Meetings/"
        f"GetMeetingFileStream(fileId={file_id},plainText=false)"
    )


def _classify_meeting_type(event: dict) -> str:
    """Map CivicClerk meetingTypeName + eventName to our canonical type."""
    name = (event.get("eventName") or "") + " " + (event.get("meetingTypeName") or "")
    for pat, label in TYPE_PATTERNS:
        if pat.search(name):
            return label
    return DEFAULT_TYPE


def _meeting_date(event: dict) -> date | None:
    """eventDate / startDateTime are ISO datetimes; take the date portion."""
    val = event.get("eventDate") or event.get("startDateTime")
    if not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _meeting_time(event: dict) -> "time | None":
    """Scheduled start time-of-day. CivicClerk stamps eventDate/startDateTime
    with a 'Z', but the value is the LOCAL wall-clock time (a 6:00 PM council
    meeting is '...T18:00:00Z', i.e. 18:00 local — not UTC), so we take the
    naive time component as-is without any timezone conversion. Midnight is
    treated as 'no time published' (the platform's default when unset)."""
    val = event.get("startDateTime") or event.get("eventDate")
    if not val:
        return None
    try:
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        return None
    t = dt.time().replace(microsecond=0)
    if t == time(0, 0, 0):
        return None
    return t


def _agenda_and_minutes(event: dict, tenant: str) -> tuple[str | None, str | None, str | None]:
    """Pick the canonical Agenda file (preferred over Agenda Packet), the Minutes
    file, and the Agenda Packet (the combined deck of supporting documents — the
    actual proposals). Returns (agenda_url, minutes_url, packet_url); any may be None."""
    agenda_file_id: int | None = None
    agenda_packet_file_id: int | None = None
    minutes_file_id: int | None = None
    for pf in event.get("publishedFiles", []):
        ptype = pf.get("type")
        fid = pf.get("fileId")
        if not fid:
            continue
        if ptype in AGENDA_TYPE_PREFERENCE and agenda_file_id is None:
            agenda_file_id = fid
        elif ptype == AGENDA_PACKET_TYPE and agenda_packet_file_id is None:
            agenda_packet_file_id = fid
        elif ptype == MINUTES_TYPE and minutes_file_id is None:
            minutes_file_id = fid
    # The packet is the deck of supporting documents — captured separately so we
    # can segment it into per-item proposals.
    packet_url = _attachment_url(tenant, agenda_packet_file_id) if agenda_packet_file_id else None
    # Fall back to the packet only if no agenda was published.
    if agenda_file_id is None:
        agenda_file_id = agenda_packet_file_id
    agenda_url = _attachment_url(tenant, agenda_file_id) if agenda_file_id else None
    minutes_url = _attachment_url(tenant, minutes_file_id) if minutes_file_id else None
    return agenda_url, minutes_url, packet_url


def parse_event(event: dict, tenant: str, category_id: int, category_name: str) -> MeetingRecord | None:
    mdate = _meeting_date(event)
    if mdate is None:
        return None
    agenda_url, minutes_url, packet_url = _agenda_and_minutes(event, tenant)
    return MeetingRecord(
        agenda_id=event["id"],
        meeting_date=mdate,
        meeting_type=_classify_meeting_type(event),
        category_id=category_id,
        category_name=category_name,
        description=(event.get("eventName") or event.get("eventDescription") or None),
        agenda_url=agenda_url,
        minutes_url=minutes_url,
        packet_url=packet_url,
        agenda_posted_at=_parse_posted_at(event.get("publishedAgendaTimeStamp")),
        meeting_time=_meeting_time(event),
    )


def inventory(
    *,
    tenant: str,
    categories: dict[int, str],
    since: "date | None" = None,
) -> Iterator[MeetingRecord]:
    """Yield MeetingRecord for every Event in every requested category.

    `categories` is {category_id: body_name} drawn from the jurisdiction
    config — same shape as the CivicEngage scraper takes. `since` restricts to
    events on/after that day (incremental); None = full history.
    """
    for cat_id, cat_name in categories.items():
        try:
            events = list(fetch_events(tenant, cat_id, since=since))
        except Exception as e:
            print(f"  ✗ fetch failed for categoryId={cat_id}: {e}", file=sys.stderr)
            continue
        for e in events:
            rec = parse_event(e, tenant, cat_id, cat_name)
            if rec is not None:
                yield rec


def main() -> int:
    from ..jurisdiction import load_config
    if len(sys.argv) != 2:
        print("usage: python -m townwatch_etl.scrapers.civicclerk_meetings <jurisdiction-slug>", file=sys.stderr)
        return 2
    cfg = load_config(sys.argv[1])
    hints = cfg.get("platform_hints", {})
    tenant = hints.get("civicclerk_tenant")
    if not tenant:
        print("config missing platform_hints.civicclerk_tenant", file=sys.stderr)
        return 1
    categories = {
        b["civicclerk"]["category_id"]: b["name"]
        for b in cfg.get("governing_bodies", [])
        if "civicclerk" in b and "category_id" in b["civicclerk"]
    }
    records = list(inventory(tenant=tenant, categories=categories))
    summary = {
        "jurisdiction": cfg["jurisdiction"]["display_name"],
        "platform": "civicclerk",
        "tenant": tenant,
        "total_meetings": len(records),
        "meetings_by_category": {
            cat: sum(1 for r in records if r.category_name == cat)
            for cat in {r.category_name for r in records}
        },
        "meetings_with_agenda": sum(1 for r in records if r.agenda_url),
        "meetings_with_minutes": sum(1 for r in records if r.minutes_url),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
