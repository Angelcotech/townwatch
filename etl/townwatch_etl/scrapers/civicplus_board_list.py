"""
Parse appointed-board member rosters from a CivicPlus CONTENT page.

The pattern (first seen: Grovetown's /164 Planning Commission & BZA page,
found by the 2026-06-12 findings re-audit): a static page where each board
is an <h2>/<h3> heading followed by a <ul> whose items read

    "Ed Connell, Vice Chair (expires December 31, 2028)"
    "Khristy Murray (expires December 31, 2026)"

This is a common CivicPlus department-page idiom (cities rarely have a
dedicated Boards & Commissions module), so the parser is generic: callers
pass the heading text that introduces the board they want.

No network here — callers fetch via http_client and pass the HTML.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from bs4 import BeautifulSoup

# "Name[, Role] (expires December 31, 2028)" — role optional, date US-long.
_ITEM_RE = re.compile(
    r"^(?P<name>[^,(]+?)"
    r"(?:,\s*(?P<role>[^()]+?))?"
    r"\s*\(\s*expires\s+(?P<date>[^)]+?)\s*\)\s*$",
    re.IGNORECASE,
)


def _parse_expiry(raw: str) -> date | None:
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_board_list(html: str, heading: str) -> list[dict]:
    """Members of the board introduced by `heading` (case-insensitive
    substring match against h1-h4 text). Returns
    [{name, role, term_expires, email, raw}] in page order.

    Handles the observed CivicPlus member-list idioms:
      * Grovetown:  "Ed Connell, Vice Chair (expires December 31, 2028)"
      * Columbia:   '<a href="mailto:mmoody@...">Email Mark Moody</a>,
                     Chairman - Countywide'  (no term dates, email present)
    """
    soup = BeautifulSoup(html, "html.parser")
    want = _norm(heading).lower()
    head = None
    for h in soup.find_all(["h1", "h2", "h3", "h4"]):
        if want in _norm(h.get_text()).lower():
            head = h
            break
    if head is None:
        raise ValueError(f"heading {heading!r} not found on page")

    # Collect <li> items from sibling content until the next same-or-higher
    # level heading — CivicPlus pages are flat, so the board's <ul> is a
    # following sibling (sometimes nested in a <p>-soup blob).
    members: list[dict] = []
    for sib in head.find_all_next():
        if sib.name in ("h1", "h2", "h3", "h4") and sib is not head:
            break
        if sib.name != "li":
            continue
        email = None
        a = sib.find("a", href=re.compile(r"^mailto:", re.I))
        if a is not None:
            email = a["href"].split(":", 1)[1].split("?")[0].strip() or None
        raw = _norm(sib.get_text())
        raw = re.sub(r"^email\s+", "", raw, flags=re.I)  # "Email Mark Moody" → "Mark Moody"
        if not raw:
            continue
        m = _ITEM_RE.match(raw)
        if m:
            members.append({
                "name": _norm(m.group("name")),
                "role": _norm(m.group("role")) if m.group("role") else None,
                "term_expires": _parse_expiry(m.group("date")),
                "email": email,
                "raw": raw,
            })
            continue
        # No "(expires ...)": "Name[, Role]" — role is everything after the
        # first comma ("Chairman - Countywide", "District 1").
        name, _, role = raw.partition(",")
        members.append({"name": _norm(name), "role": _norm(role) or None,
                        "term_expires": None, "email": email, "raw": raw})
    return members
