"""
CivicEngage AgendaCenter — meeting inventory scraper.

Generic to any CivicEngage / CivicPlus AgendaCenter installation. The
caller supplies base_url and a {category_id: body_name} map drawn from
the jurisdiction config.

Run standalone (Grovetown example):
    python -m townwatch_etl.scrapers.civicengage_agendacenter grovetown-ga
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterator, Optional  # noqa: F401  (kept for older callers)

import httpx
from bs4 import BeautifulSoup


USER_AGENT = "TownWatch-ETL/0.1 (civic transparency research)"
REQUEST_DELAY_SECS = 1.0

# CivicEngage prints "Posted [Month] [Day], [Year] [Time AM/PM]" next to
# each agenda link. Captures the timestamp the city actually uploaded
# the document — diff vs. meeting_date is the citizen-notice signal.
POSTED_RE = re.compile(
    r"Posted\s+([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})\s+(\d{1,2}):(\d{2})\s*([AP]M)",
    re.IGNORECASE,
)

_MONTH_LOOKUP = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def _parse_posted_at(row_text: str) -> datetime | None:
    """Parse the 'Posted Month D, YYYY H:MM AM/PM' phrase if present.

    Returns naive datetime (no tz). CivicEngage timestamps are local to
    the city; callers can assume the jurisdiction's local zone when this
    matters for notice-threshold calculations.
    """
    m = POSTED_RE.search(row_text)
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

TYPE_PATTERNS = [
    (re.compile(r"\bwork session\b", re.I),         "workshop"),
    (re.compile(r"\bspecial(\s+called)?\b", re.I),  "special"),
    (re.compile(r"\bemergency\b", re.I),            "emergency"),
    (re.compile(r"\bexecutive session\b", re.I),    "executive_session"),
]
DEFAULT_TYPE = "regular"

DATE_ID_RE = re.compile(r"_(\d{2})(\d{2})(\d{4})-(\d+)")


@dataclass
class MeetingRecord:
    agenda_id: int
    meeting_date: date
    meeting_type: str
    category_id: int
    category_name: str
    description: str | None
    agenda_url: str
    minutes_url: str | None
    agenda_posted_at: datetime | None = None


def fetch_year(base_url: str, category_id: int, year: int) -> str:
    """Fetch the HTML fragment for one (body, year) on a CivicEngage site."""
    endpoint = f"{base_url}/AgendaCenter/UpdateCategoryList"
    with httpx.Client(timeout=30.0, headers={"User-Agent": USER_AGENT}) as client:
        r = client.post(
            endpoint,
            data={"year": year, "catID": category_id},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        r.raise_for_status()
        return r.text


def parse_rows(
    html: str,
    base_url: str,
    category_id: int,
    category_name: str,
) -> list[MeetingRecord]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[MeetingRecord] = []

    for tr in soup.select("tr.catAgendaRow"):
        agenda_link = tr.find("a", href=re.compile(r"/AgendaCenter/ViewFile/Agenda/_\d{8}-\d+"))
        if agenda_link is None:
            continue
        m = DATE_ID_RE.search(agenda_link["href"])
        if not m:
            continue
        mm, dd, yyyy, agenda_id = m.groups()
        meeting_date = date(int(yyyy), int(mm), int(dd))

        description = agenda_link.get_text(strip=True) or None
        # CivicEngage sometimes appends ?html=true to route links through
        # their HTML viewer instead of returning the raw document. Always
        # strip the query string so downstream extractors get the file.
        agenda_url = base_url + agenda_link["href"].split("?", 1)[0]

        minutes_anchor = tr.find("a", href=re.compile(r"/AgendaCenter/ViewFile/Minutes/_\d{8}-\d+"))
        minutes_url = base_url + minutes_anchor["href"].split("?", 1)[0] if minutes_anchor else None

        meeting_type = DEFAULT_TYPE
        if description:
            for pat, label in TYPE_PATTERNS:
                if pat.search(description):
                    meeting_type = label
                    break

        # Posted-on timestamp lives elsewhere in the row, not on the
        # anchor itself. Read all text inside the <tr> so the regex
        # picks it up regardless of which <td> CivicEngage renders it in.
        posted_at = _parse_posted_at(tr.get_text(" ", strip=True))

        out.append(MeetingRecord(
            agenda_id=int(agenda_id),
            meeting_date=meeting_date,
            meeting_type=meeting_type,
            category_id=category_id,
            category_name=category_name,
            description=description,
            agenda_url=agenda_url,
            minutes_url=minutes_url,
            agenda_posted_at=posted_at,
        ))

    out.sort(key=lambda r: (r.meeting_date, r.agenda_id))
    return out


def inventory(
    *,
    base_url: str,
    categories: dict[int, str],
    year_range: tuple[int, int] = (2012, datetime.now().year),
) -> Iterator[MeetingRecord]:
    """Yield every meeting record across the specified categories and years."""
    start_year, end_year = year_range
    for cat_id, cat_name in categories.items():
        for year in range(start_year, end_year + 1):
            try:
                html = fetch_year(base_url, cat_id, year)
            except Exception as e:
                print(f"  ✗ fetch failed for catID={cat_id} year={year}: {e}", file=sys.stderr)
                continue
            rows = parse_rows(html, base_url, cat_id, cat_name)
            for row in rows:
                yield row
            time.sleep(REQUEST_DELAY_SECS)


def main() -> int:
    from ..jurisdiction import load_config
    if len(sys.argv) != 2:
        print("usage: python -m townwatch_etl.scrapers.civicengage_agendacenter <jurisdiction-slug>", file=sys.stderr)
        return 2
    cfg = load_config(sys.argv[1])
    hints = cfg.get("platform_hints", {})
    base_url = hints.get("agenda_base_url")
    if not base_url:
        print("config missing platform_hints.agenda_base_url", file=sys.stderr)
        return 1
    categories = {
        b["civicengage"]["category_id"]: b["name"]
        for b in cfg.get("governing_bodies", [])
        if "civicengage" in b and "category_id" in b["civicengage"]
    }
    records = list(inventory(base_url=base_url, categories=categories))
    summary = {
        "jurisdiction": cfg["jurisdiction"]["display_name"],
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "total_meetings": len(records),
        "meetings_by_category": {
            cat: sum(1 for r in records if r.category_name == cat)
            for cat in set(r.category_name for r in records)
        },
        "meetings_with_minutes": sum(1 for r in records if r.minutes_url),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
