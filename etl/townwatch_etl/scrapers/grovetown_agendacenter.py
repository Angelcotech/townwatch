"""
Grovetown AgendaCenter — meeting inventory scraper.

The CivicEngage AgendaCenter exposes a year-filtered list of meetings via
an AJAX endpoint. We POST {year, catID} to /AgendaCenter/UpdateCategoryList
and parse the returned HTML fragment for meeting rows.

City Council catID = 2.
Available years for Grovetown: 2012 through current.

Run standalone:
    python -m townwatch_etl.scrapers.grovetown_agendacenter
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterator

import httpx
from bs4 import BeautifulSoup


BASE_URL = "https://cityofgrovetown.com"
INVENTORY_ENDPOINT = f"{BASE_URL}/AgendaCenter/UpdateCategoryList"
USER_AGENT = "TownWatch-ETL/0.1 (civic transparency research)"

# Body categories — discovered from inspecting the AgendaCenter page
CATEGORIES = {
    2: "City Council",
    5: "Planning Commission",
    6: "Board of Zoning Appeals",
}

# Polite delay between requests so we don't hammer the server
REQUEST_DELAY_SECS = 1.0

# Meeting type inferred from the description text
TYPE_PATTERNS = [
    (re.compile(r"\bwork session\b", re.I),    "workshop"),
    (re.compile(r"\bspecial(\s+called)?\b", re.I), "special"),
    (re.compile(r"\bemergency\b", re.I),       "emergency"),
    (re.compile(r"\bexecutive session\b", re.I),"executive_session"),
]
DEFAULT_TYPE = "regular"

# URL pattern: _MMDDYYYY-{agendaID}
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


def fetch_year(category_id: int, year: int) -> str:
    """Fetch the HTML fragment for one (body, year) combination."""
    with httpx.Client(timeout=30.0, headers={"User-Agent": USER_AGENT}) as client:
        r = client.post(
            INVENTORY_ENDPOINT,
            data={"year": year, "catID": category_id},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        r.raise_for_status()
        return r.text


def parse_rows(html: str, category_id: int, category_name: str) -> list[MeetingRecord]:
    """Parse meeting rows from a year fragment."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[MeetingRecord] = []

    for tr in soup.select("tr.catAgendaRow"):
        # Find the agenda link with the canonical /_MMDDYYYY-{id} URL
        agenda_link = tr.find("a", href=re.compile(r"/AgendaCenter/ViewFile/Agenda/_\d{8}-\d+"))
        if agenda_link is None:
            continue

        m = DATE_ID_RE.search(agenda_link["href"])
        if not m:
            continue
        mm, dd, yyyy, agenda_id = m.groups()
        meeting_date = date(int(yyyy), int(mm), int(dd))

        description = agenda_link.get_text(strip=True) or None
        agenda_url = BASE_URL + agenda_link["href"]

        minutes_anchor = tr.find("a", href=re.compile(r"/AgendaCenter/ViewFile/Minutes/_\d{8}-\d+"))
        minutes_url = BASE_URL + minutes_anchor["href"] if minutes_anchor else None

        meeting_type = DEFAULT_TYPE
        if description:
            for pat, label in TYPE_PATTERNS:
                if pat.search(description):
                    meeting_type = label
                    break

        out.append(MeetingRecord(
            agenda_id=int(agenda_id),
            meeting_date=meeting_date,
            meeting_type=meeting_type,
            category_id=category_id,
            category_name=category_name,
            description=description,
            agenda_url=agenda_url,
            minutes_url=minutes_url,
        ))

    # Newest first within the year — return chronological
    out.sort(key=lambda r: (r.meeting_date, r.agenda_id))
    return out


def inventory(
    category_ids: list[int] | None = None,
    year_range: tuple[int, int] = (2012, datetime.now().year),
) -> Iterator[MeetingRecord]:
    """Yield every meeting record across the specified bodies and years."""
    cats = category_ids or list(CATEGORIES.keys())
    start_year, end_year = year_range

    for cat_id in cats:
        cat_name = CATEGORIES.get(cat_id, f"category_{cat_id}")
        for year in range(start_year, end_year + 1):
            try:
                html = fetch_year(cat_id, year)
            except Exception as e:
                print(f"  ✗ fetch failed for catID={cat_id} year={year}: {e}", file=sys.stderr)
                continue
            rows = parse_rows(html, cat_id, cat_name)
            for row in rows:
                yield row
            time.sleep(REQUEST_DELAY_SECS)


def main() -> int:
    all_records = list(inventory())
    result = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "source_base": BASE_URL,
        "category_ids": list(CATEGORIES.keys()),
        "total_meetings": len(all_records),
        "meetings_by_category": {
            cat: sum(1 for r in all_records if r.category_name == cat)
            for cat in set(r.category_name for r in all_records)
        },
        "meetings_with_minutes": sum(1 for r in all_records if r.minutes_url),
        "meetings": [
            {
                "agenda_id": r.agenda_id,
                "meeting_date": r.meeting_date.isoformat(),
                "meeting_type": r.meeting_type,
                "category": r.category_name,
                "category_id": r.category_id,
                "description": r.description,
                "agenda_url": r.agenda_url,
                "minutes_url": r.minutes_url,
            }
            for r in all_records
        ],
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
