"""
CivicEngage-pattern officials table scraper.

Generic to CivicEngage / CivicPlus city sites where the council/board
roster is a 4-column HTML table (Member, Position, Address, Phone) with
the telerik-reTable styling. The caller supplies the page URL from the
jurisdiction config (data_sources.officials_roster.url).

Run standalone:
    python -m townwatch_etl.scrapers.civicengage_officials_table <jurisdiction-slug>
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from typing import TypedDict

from ..http_client import civic_get
from bs4 import BeautifulSoup


USER_AGENT = "TownWatch-ETL/0.1 (civic transparency research)"
TABLE_CLASS = "telerik-reTable-2"


class OfficialRecord(TypedDict):
    raw_name: str
    is_vacant: bool
    position: str
    address: str | None
    phone: str | None
    directory_eid: int | None


class ScrapeResult(TypedDict):
    source_url: str
    scraped_at: str
    body_meta: dict[str, str]
    officials: list[OfficialRecord]


def fetch_html(url: str) -> str:
    r = civic_get(url, timeout=30.0)
    r.raise_for_status()
    return r.text


def parse_officials(html: str) -> list[OfficialRecord]:
    """Parse the 4-column council member table (Member, Position, Address, Phone)."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_=TABLE_CLASS)
    if table is None:
        raise ValueError(f"Could not find officials table with class={TABLE_CLASS}")

    out: list[OfficialRecord] = []
    for row in table.select("tbody > tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        name_cell = cells[0]
        position = _clean(cells[1].get_text(" ", strip=True))
        address = _clean(cells[2].get_text(" ", strip=True))
        phone = _clean(cells[3].get_text(" ", strip=True))

        raw_name = _clean(name_cell.get_text(" ", strip=True))
        is_vacant = raw_name.upper() == "VACANT"

        link = name_cell.find("a", href=True)
        eid = _extract_eid(link["href"]) if link else None

        out.append(OfficialRecord(
            raw_name=raw_name,
            is_vacant=is_vacant,
            position=position,
            address=None if address in ("", "N/A") else address,
            phone=phone or None,
            directory_eid=eid,
        ))

    return out


def parse_body_meta(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    meta: dict[str, str] = {}

    members_h2 = soup.find("h2", string=re.compile(r"Members", re.I))
    if members_h2:
        p = members_h2.find_next("p")
        if p:
            meta["governance_description"] = _clean(p.get_text(" ", strip=True))

    meetings_h2 = soup.find("h2", string=re.compile(r"Meetings", re.I))
    if meetings_h2:
        ul = meetings_h2.find_next("ul")
        if ul:
            items = [_clean(li.get_text(" ", strip=True)) for li in ul.find_all("li")]
            meta["meeting_schedule"] = " | ".join(items)

    return meta


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _extract_eid(href: str) -> int | None:
    m = re.search(r"EID=(\d+)", href)
    return int(m.group(1)) if m else None


def scrape(url: str) -> ScrapeResult:
    html = fetch_html(url)
    return ScrapeResult(
        source_url=url,
        scraped_at=datetime.now(timezone.utc).isoformat(),
        body_meta=parse_body_meta(html),
        officials=parse_officials(html),
    )


def main() -> int:
    from ..jurisdiction import load_config
    if len(sys.argv) != 2:
        print("usage: python -m townwatch_etl.scrapers.civicengage_officials_table <jurisdiction-slug>", file=sys.stderr)
        return 2
    cfg = load_config(sys.argv[1])
    url = cfg.get("data_sources", {}).get("officials_roster", {}).get("url")
    if not url:
        print("config missing data_sources.officials_roster.url", file=sys.stderr)
        return 1
    try:
        result = scrape(url)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
