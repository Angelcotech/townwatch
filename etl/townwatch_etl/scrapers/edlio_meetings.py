"""
Edlio CMS meetings scraper (e.g. Columbia County School District).

Edlio is a school-website CMS, not a board-management platform. There is no API
and no per-category event feed. Instead a board posts a flat list of links on
plain pages:
  - a MINUTES page  — anchors like "6-10-2025 Regular Session Meeting Minutes"
                       → a Google Doc (docs.google.com/document/d/<id>/edit)
  - an AGENDAS page — same shape, when the board posts agendas at all
                       (Columbia County's agendas page is empty — that absence is
                       itself an Open-Meetings-Act finding, not a scraper bug).

We parse each page into (meeting_date → doc link), JOIN the two by date, and yield
one MeetingRecord per meeting. Document links are Google Docs, so we hand back the
`/export?format=pdf` URL — `http_client.civic_get` follows Google's redirect to
googleusercontent and the existing PDF extractor handles it unchanged. Dead docs
(some links 410-Gone) are left to the per-meeting isolation downstream.

Incremental: these are full listing pages with no server-side date filter, so
`since` is applied client-side (yield only meetings on/after the cutoff).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Iterator

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..http_client import civic_get


@dataclass
class MeetingRecord:
    agenda_id: int                       # synthetic stable id (YYYYMMDD)
    meeting_date: date
    meeting_type: str
    category_id: int
    category_name: str
    description: str | None
    agenda_url: str | None               # Google-Docs export PDF, or None
    minutes_url: str | None              # Google-Docs export PDF, or None
    agenda_posted_at: datetime | None = None
    meeting_time: time | None = None
    location: str | None = None
    packet_url: str | None = None


# Anchor text looks like "M-D-YYYY  <Session words>  Meeting Minutes/Agenda".
# Some pages (Meeting Presentations) abbreviate the year: "4-21-26 Regular
# Session Meeting Presentation" — accept 2-digit years and pivot to 20xx.
_ROW_RE = re.compile(r"^\s*(\d{1,2})-(\d{1,2})-(\d{2,4})\b(.*)$")
_DOC_ID_RE = re.compile(r"/document/d/([A-Za-z0-9_-]+)")
# Year-subpage nav inside an Edlio section: anchor text like "2024-2025".
_YEAR_PAGE_RE = re.compile(r"^\s*20\d{2}\s*-\s*20\d{2}\s*$")
_UREC_RE = re.compile(r"[?&]uREC_ID=(\d+)")

# Session phrasing → canonical meeting_type. Order matters (first match wins).
_TYPE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"work\s*session", re.I), "workshop"),
    (re.compile(r"emergency", re.I), "emergency"),
    (re.compile(r"special|called", re.I), "special"),
    (re.compile(r"executive", re.I), "executive_session"),
    (re.compile(r"regular", re.I), "regular"),
]


def _classify(text: str) -> str:
    for pat, label in _TYPE_PATTERNS:
        if pat.search(text):
            return label
    return "regular"


def _export_pdf_url(doc_href: str) -> str | None:
    """Google-Docs edit/view link → public PDF export URL."""
    m = _DOC_ID_RE.search(doc_href)
    if not m:
        return None
    return f"https://docs.google.com/document/d/{m.group(1)}/export?format=pdf"


def _doc_url(href: str) -> str | None:
    """A fetchable document URL from a listing anchor — Google Docs become the
    PDF export URL; direct PDF links (e.g. files.edl.io) pass through as-is."""
    if "docs.google.com/document" in href:
        return _export_pdf_url(href)
    if href.lower().split("?", 1)[0].endswith(".pdf"):
        return href
    return None


def _collect_docs(soup: BeautifulSoup, out: dict[date, dict]) -> None:
    """Accumulate {meeting_date: {url, type, desc}} from one listing page.

    On a date collision (e.g. two sessions same day) the LAST seen wins; the
    board's pages list one document per meeting so this is rare."""
    for a in soup.find_all("a", href=True):
        doc = _doc_url(a["href"])
        if not doc:
            continue
        text = " ".join(a.get_text().split())
        m = _ROW_RE.match(text)
        if not m:
            continue
        mm, dd, yy, rest = m.group(1), m.group(2), m.group(3), m.group(4)
        try:
            year = int(yy)
            if year < 100:
                year += 2000
            mdate = date(year, int(mm), int(dd))
        except ValueError:
            continue  # malformed date in the anchor text — skip
        out[mdate] = {"url": doc, "type": _classify(rest), "desc": text}


def _year_subpages(soup: BeautifulSoup, base_url: str) -> list[str]:
    """School-year subpages ("2024-2025", "2025-2026", …) within the SAME
    Edlio section as base_url. Edlio boards split each document kind across
    one subpage per school year; a config that points at any one page (or the
    section root) would silently miss every other year — CCSD's agendas were
    declared "not published" for exactly this reason when only an empty
    landing subpage was checked. Same-section = same uREC_ID."""
    m = _UREC_RE.search(base_url)
    if not m:
        return []
    urec = m.group(1)
    pages: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        um = _UREC_RE.search(href)
        if not um or um.group(1) != urec:
            continue
        if not _YEAR_PAGE_RE.match(a.get_text() or ""):
            continue
        full = urljoin(base_url, href)
        if full != base_url and full not in pages:
            pages.append(full)
    return pages


def _parse_listing(url: str) -> dict[date, dict]:
    """Fetch a minutes/agendas/presentations listing → {meeting_date: doc}.

    Fetches the configured page, then sweeps every school-year subpage of the
    same section, merging all years into one date-keyed map."""
    out: dict[date, dict] = {}
    if not url:
        return out
    r = civic_get(url, timeout=60.0)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    _collect_docs(soup, out)
    for sub in _year_subpages(soup, url):
        try:
            rs = civic_get(sub, timeout=60.0)
            rs.raise_for_status()
            _collect_docs(BeautifulSoup(rs.text, "html.parser"), out)
        except Exception as e:
            # One bad year page must not lose the other 14 years.
            print(f"  ⚠ edlio: year subpage failed ({e}) — {sub}")
    return out


def inventory(
    *,
    category_id: int,
    category_name: str,
    minutes_url: str | None = None,
    agendas_url: str | None = None,
    presentations_url: str | None = None,
    schedule_url: str | None = None,  # reserved; CCSD's is a text table, not parsed in v1
    since: "date | None" = None,
) -> Iterator[MeetingRecord]:
    """Yield one MeetingRecord per meeting, joining the agendas + minutes +
    presentations pages by date (presentations — slide decks / proposals shown
    at the meeting — ride along as packet_url). `since` filters client-side.
    `schedule_url` (upcoming dates) is not parsed in v1."""
    agendas = _parse_listing(agendas_url)
    minutes = _parse_listing(minutes_url)
    presentations = _parse_listing(presentations_url)

    for mdate in sorted(set(agendas) | set(minutes) | set(presentations), reverse=True):
        if since is not None and mdate < since:
            continue
        ag = agendas.get(mdate)
        mn = minutes.get(mdate)
        pr = presentations.get(mdate)
        # Prefer the agenda's type/description (it's the forward-looking record);
        # fall back to the minutes', then the presentation's.
        meta = ag or mn or pr
        yield MeetingRecord(
            agenda_id=int(mdate.strftime("%Y%m%d")),
            meeting_date=mdate,
            meeting_type=meta["type"],
            category_id=category_id,
            category_name=category_name,
            description=meta["desc"],
            agenda_url=ag["url"] if ag else None,
            minutes_url=mn["url"] if mn else None,
            packet_url=pr["url"] if pr else None,
        )
