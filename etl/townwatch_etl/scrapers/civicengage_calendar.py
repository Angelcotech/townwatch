"""
CivicEngage (CivicPlus) calendar scraper — FORWARD-LOOKING meetings.

AgendaCenter (civicengage_agendacenter.py) only surfaces a meeting once its
agenda is POSTED — days before, sometimes not at all — so cities had no upcoming
meetings until then. The public events Calendar lists meetings further out, with
date / time / location. This scraper reads the calendar's LIST view per month,
keeps only events whose title matches a governing body, and pulls each meeting's
structured fields from its per-event iCal export:

    DTSTART;TZID=America/New_York:20260511T180000   → date + LOCAL time (the TZID
        makes the wall-clock unambiguous — no UTC guesswork)
    LOCATION: ...
    SUMMARY: 5/11/2026 City Council Meeting

Produces CalendarMeeting with agenda_url=None; when the agenda is later posted,
the AgendaCenter scraper fills it in on the same (body, date) row (the inventory
upsert reconciles a calendar pre-seed instead of duplicating).

This is its own module (the calendar is a different source than AgendaCenter);
the inventory job runs both for a CivicEngage city.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dtime
from typing import Iterator

from ..http_client import civic_get

REQUEST_DELAY_SECS = 0.5

_LIST_URL = "{base}/calendar.aspx?view=list&year={y}&month={m}"
_ICAL_URL = "{base}/common/modules/iCalendar/iCalendar.aspx?feed=calendar&eventID={eid}"
# Anchor text inside the list view: an EID link wrapping the event title. The
# inner (?:<[^>]+>\s*)* skips any nested spans before the title text.
_EVENT_RE = re.compile(r"Calendar\.aspx\?EID=(\d+)[^>]*>\s*(?:<[^>]+>\s*)*([^<]{3,80})")


@dataclass
class CalendarMeeting:
    event_id: int
    meeting_date: date
    meeting_time: dtime | None
    location: str | None
    title: str
    body_name: str            # the governing body the title matched


def _list_events(base_url: str, year: int, month: int) -> list[tuple[int, str]]:
    """(event_id, title) for every event in one month's list view."""
    html = civic_get(_LIST_URL.format(base=base_url, y=year, m=month), timeout=30.0).text
    out: list[tuple[int, str]] = []
    seen: set[int] = set()
    for eid_s, raw in _EVENT_RE.findall(html):
        eid = int(eid_s)
        title = re.sub(r"\s+", " ", raw).strip()
        if not title or eid in seen or title.lower().startswith("event det"):
            continue
        seen.add(eid)
        out.append((eid, title))
    return out


def _vevent(ics: str) -> str:
    """The VEVENT block only. Critical: the iCal also has a VTIMEZONE block whose
    DST transitions carry their OWN bare `DTSTART:` lines — parsing the whole
    document would pick up a 2024 DST date instead of the event's real start."""
    i = ics.find("BEGIN:VEVENT")
    if i == -1:
        return ""
    j = ics.find("END:VEVENT", i)
    return ics[i: j if j != -1 else len(ics)]


def _ics_field(ics: str, key: str) -> str | None:
    m = re.search(rf"^{key}[^:\n]*:(.+)$", ics, re.M)
    return m.group(1).strip() if m else None


def _parse_dtstart(ics: str) -> tuple[date | None, dtime | None]:
    """DTSTART carries the LOCAL wall-clock (with a TZID), so take the date and
    time components directly. Midnight = all-day / no specific time → None."""
    m = re.search(r"DTSTART[^:\n]*:(\d{8})T?(\d{6})?", ics)
    if not m:
        return None, None
    d = m.group(1)
    dd = date(int(d[0:4]), int(d[4:6]), int(d[6:8]))
    t = m.group(2)
    tt = None
    if t and t != "000000":
        tt = dtime(int(t[0:2]), int(t[2:4]))
    return dd, tt


def _match_body(title: str, body_keywords: dict[str, list[str]]) -> str | None:
    """Return the body whose keywords the event title matches, else None (so
    community events like 'Father's Day Lunch' are skipped)."""
    low = title.lower()
    for body_name, kws in body_keywords.items():
        if any(kw in low for kw in kws):
            return body_name
    return None


def upcoming_meetings(
    *,
    base_url: str,
    body_keywords: dict[str, list[str]],
    months_ahead: int = 4,
    from_date: date | None = None,
) -> Iterator[CalendarMeeting]:
    """Yield governing-body meetings on/after from_date across this month and the
    next `months_ahead` months. body_keywords maps a governing body's name to the
    lowercase substrings that identify its meetings in event titles."""
    start = from_date or datetime.now().date()
    y, m = start.year, start.month
    for _ in range(months_ahead + 1):
        try:
            events = _list_events(base_url, y, m)
        except Exception as e:  # one month failing shouldn't kill the rest
            print(f"  ✗ calendar list fetch failed {m}/{y}: {e}", file=sys.stderr)
            events = []
        for eid, title in events:
            body = _match_body(title, body_keywords)
            if body is None:
                continue
            try:
                ics = civic_get(_ICAL_URL.format(base=base_url, eid=eid), timeout=20.0).text
            except Exception:
                continue
            ev = _vevent(ics)  # VEVENT only — avoids the VTIMEZONE DTSTART trap
            dd, tt = _parse_dtstart(ev)
            if dd is None or dd < start:
                continue
            loc = _ics_field(ev, "LOCATION")
            if loc:
                loc = re.sub(r"^[-\s]+", "", loc).strip() or None  # CivicPlus prefixes "- "
            yield CalendarMeeting(eid, dd, tt, loc, title, body)
            time.sleep(REQUEST_DELAY_SECS)
        m += 1
        if m > 12:
            m, y = 1, y + 1


def default_body_keywords(governing_bodies: list[dict]) -> dict[str, list[str]]:
    """Derive title-match keywords from each body's name (+ a couple of common
    variants). Overridable via a body's config: civicengage.calendar_keywords."""
    out: dict[str, list[str]] = {}
    for b in governing_bodies:
        name = b["name"]
        ce = b.get("civicengage", {})
        if ce.get("calendar_keywords"):
            out[name] = [k.lower() for k in ce["calendar_keywords"]]
            continue
        kws = {name.lower()}
        low = name.lower()
        if "zoning" in low:
            kws.update({"zoning appeals", "zoning board", "bza"})
        if "planning" in low:
            kws.add("planning commission")
        if "council" in low:
            kws.add("council meeting")
        out[name] = sorted(kws)
    return out
