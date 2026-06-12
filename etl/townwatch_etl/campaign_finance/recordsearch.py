"""
Client for Georgia's campaign-finance record-search API.

recordsearch.ethics.ga.gov (the Government Transparency and Campaign Finance
Commission's 2022–2025 system) exposes unauthenticated JSON endpoints under
api-recordsearch.ethics.ga.gov/api/PublicFilerDetails. Every Georgia LOCAL
filing office (city/county clerk registered as "Local Filing Officer")
uploads scanned candidate filings there, so one client covers municipal
campaign finance statewide — the channel that makes pre-2027 paper filings
machine-readable. 2026+ filings live in peachfile.ethics.ga.gov (JS-only
SPA) — out of scope here.

Endpoints (verified live 2026-06-12):
  POST GetCommitteeDetails   {filerName, fcName:"Local Filing Officer",
                              pageNumber, pageSize} → filing offices
  POST GetPublicDocumentList {filerRegistrationGuid, pageNumber, pageSize}
                             → document metadata (guid, documentName,
                               documentType, dateReceived)
  GET  PublicDownloadFile/?documentGuid=<guid> → the PDF (usually a scan)

All requests go through the shared throttled http_client (chokepoint rule).
"""

from __future__ import annotations

import re
from datetime import datetime

from ..http_client import civic_get, civic_post

API_BASE = "https://api-recordsearch.ethics.ga.gov/api/PublicFilerDetails"
_PAGE_SIZE = 100


def resolve_filing_office(filer_name: str) -> dict | None:
    """The local filing office whose filerName matches (exact, case-insensitive)
    — e.g. 'Grovetown', 'Columbia County'. None when the jurisdiction has no
    office registered in the record-search system (its filings are paper-only
    at the clerk's desk; absence of an office is NOT absence of filings)."""
    r = civic_post(
        f"{API_BASE}/GetCommitteeDetails",
        json={"filerName": filer_name, "fcName": "Local Filing Officer",
              "pageNumber": 1, "pageSize": 20},
        timeout=30.0,
    )
    r.raise_for_status()
    items = (r.json().get("data") or {}).get("items") or []
    want = filer_name.strip().lower()
    for it in items:
        if (it.get("filerName") or "").strip().lower() == want:
            return it
    return None


def list_documents(filer_registration_guid: str) -> list[dict]:
    """All public documents for a filing office, fully paged."""
    out: list[dict] = []
    page = 1
    while True:
        r = civic_post(
            f"{API_BASE}/GetPublicDocumentList",
            json={"filerRegistrationGuid": filer_registration_guid,
                  "pageNumber": page, "pageSize": _PAGE_SIZE},
            timeout=30.0,
        )
        r.raise_for_status()
        data = r.json().get("data") or {}
        items = data.get("items") or []
        out.extend(items)
        total = data.get("totalItems") or 0
        if len(out) >= total or not items:
            return out
        page += 1


def document_url(document_guid: str) -> str:
    return f"{API_BASE}/PublicDownloadFile/?documentGuid={document_guid}"


def download_document(document_guid: str) -> bytes:
    r = civic_get(document_url(document_guid), timeout=120.0)
    r.raise_for_status()
    return r.content


# ── document classification ──────────────────────────────────────────────────

# kind → how the ingestor treats it:
#   ccdr                 → contribution extraction (the paper trail itself)
#   exemption_affidavit  → filing-exists record (declared activity < $2,500)
#   pfds / doi / notice  → filing-exists record (candidacy paperwork)
#   election_outcome     → skipped here (elections domain's food, not ours)
#   other                → filing-exists record with raw metadata
def classify_document(doc: dict) -> str:
    dtype = (doc.get("documentType") or "").lower()
    name = (doc.get("documentName") or "").lower()
    if "ccdr" in dtype or "ccdr" in name or "contribution disclosure" in name:
        return "ccdr"
    if "intent not to exceed" in dtype or "affidavit of exemption" in name or "exemption" in name:
        return "exemption_affidavit"
    if "personal financial disclosure" in dtype or "pfds" in name:
        return "pfds"
    if "declaration of intent" in dtype or re.search(r"\bdoi\b", name):
        return "doi"
    if "notice of candidacy" in dtype or "notice of candidacy" in name:
        return "notice_of_candidacy"
    if "election outcome" in name or "election results" in name:
        return "election_outcome"
    return "other"


# The clerk's separator is '-', but surnames can be hyphenated too
# ("Jacqueline Rivera-Player-2025 ..."): allow one optional ALPHABETIC
# hyphen-joined segment before the separator — a digit after the hyphen
# (the year/date) never matches it.
_NAME_PREFIX_RE = re.compile(
    r"^\s*([A-Za-z][A-Za-z.' ]+?"
    r"(?:-(?!(?i:CCDR|PFDS|DOI|Form|Affidavit|Notice|Registration|Councilmember|Mayor)\b)"
    r"[A-Za-z][A-Za-z.']+)?)\s*[-–—]")
_YEAR_RE = re.compile(r"\b(20\d{2})\b")


def parse_document_name(name: str) -> dict:
    """Best-effort person + election-cycle year from the clerk's document
    title (convention: 'First Last- <description with dates>'). Clerk-typed,
    so both fields may be None — callers must handle unresolved filers."""
    person = None
    m = _NAME_PREFIX_RE.match(name or "")
    if m:
        cand = m.group(1).strip()
        # Reject prefixes that are clearly not person names.
        if 2 <= len(cand.split()) <= 4 and not re.search(
                r"\b(election|outcome|city|county|council|board)\b", cand, re.I):
            person = cand
    years = [int(y) for y in _YEAR_RE.findall(name or "")]
    return {"person": person, "cycle_year": max(years) if years else None}


_FILING_TYPE_HINTS = [
    (re.compile(r"december\s+31|june\s+30|non-?election\s+year", re.I), "semi_annual"),
    (re.compile(r"amend", re.I), "amendment"),
    (re.compile(r"final|termination", re.I), "final"),
]


def infer_filing_type(doc_name: str, date_received: datetime | None) -> str:
    """Map a CCDR's title to the campaign_filing.filing_type vocabulary.
    Conservative: only the unambiguous patterns; everything else is 'other'
    (the title and raw metadata are preserved on the row)."""
    for pat, ftype in _FILING_TYPE_HINTS:
        if pat.search(doc_name or ""):
            return ftype
    if date_received is not None and date_received.month in (9, 10):
        # GA municipal general elections are early November; Sept/Oct CCDRs
        # in any year are overwhelmingly pre-election reports.
        return "pre_election"
    return "other"
