"""
Auto-discover a jurisdiction's Board-of-Elections results page.

Unlike GIS districts (which mostly live on standardized ArcGIS REST
endpoints), election-results pages don't follow a single platform
convention — Clarity Elections is dominant in some states (Georgia,
Ohio), but counties also self-host on /elections, /boe, /vote, or
under subdomains like elections.{county}.gov.

This job uses a URL-pattern probe ladder under the jurisdiction's
official_website, scoring each candidate by page content (does it
actually look like an elections office page?). The proposed URL gets
written to jurisdictions/{slug}.json under elections.results_endpoint
with a confidence tag the operator can verify and promote.

Strategy:
  1. Pattern probe — common URL shapes under official_website
  2. Content scoring — does the response mention election-results terms?
  3. (Future) State-provider lookup — Clarity Elections directory search

Run:
    python -m townwatch_etl.jobs.discover_election_source --slug columbia-county-ga
    python -m townwatch_etl.jobs.discover_election_source --slug columbia-county-ga --apply
    python -m townwatch_etl.jobs.discover_election_source                  # all configs
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Literal, Optional
from urllib.parse import urljoin, urlparse

from ..http_client import civic_client

from ..jurisdiction import JURISDICTIONS_DIR, list_slugs, load_config


Confidence = Literal["high", "medium", "low"]
USER_AGENT = "TownWatch-discover-elections/0.1 (civic transparency research)"

# URL patterns to probe under a jurisdiction's official_website.
# Order matters — most-likely shapes first. Each pattern is appended to
# the website's origin (or subdomain-substituted for the host-prefix forms).
PATH_PATTERNS = [
    "/elections",
    "/government/elections",
    "/government/board-of-elections",
    "/government/board-of-elections-and-registration",
    "/departments/elections",
    "/departments/board-of-elections",
    "/boe",
    "/vote",
    "/voting",
    "/election-results",
    "/results",
]

HOST_PATTERNS = ["elections.{host}", "vote.{host}", "boe.{host}"]

# Keywords that signal a real elections-office page. The more present,
# the higher the confidence.
ELECTION_SIGNAL_KEYWORDS = [
    "board of elections",
    "election results",
    "certified",
    "candidates",
    "voter registration",
    "polling",
    "absentee",
    "early voting",
    "ballot",
    "precinct",
    "elections office",
]

# Negative signals — page is talking about elections but isn't the
# elections office (e.g. a campaign press release).
NEGATIVE_KEYWORDS = [
    "campaign donation",
    "endorse",
    "press release",
]


@dataclass
class ElectionSourceProposal:
    url: str
    confidence: Confidence
    method: str
    score: int = 0
    title: str = ""
    matched_keywords: list[str] = None  # type: ignore

    def __post_init__(self):
        if self.matched_keywords is None:
            self.matched_keywords = []


def _score_page(html: str, url: str = "") -> tuple[int, list[str]]:
    text = html.lower()
    url_lower = url.lower()
    matched: list[str] = []
    score = 0
    for kw in ELECTION_SIGNAL_KEYWORDS:
        if kw in text:
            matched.append(kw)
            score += 1
    for neg in NEGATIVE_KEYWORDS:
        if neg in text:
            score -= 1
    # URL-shape bonus: pages whose URL contains "result" are almost
    # certainly the results LISTING (the ingest target) — that's the
    # name boards-of-elections universally give to certified-results
    # pages. Big bonus so it dominates the office-info page which
    # often has more keyword density overall.
    if "result" in url_lower:
        score += 12
        matched.append("[url has 'result']")
    if url_lower.endswith(".pdf"):
        score += 15
        matched.append("[direct PDF]")
    return score, matched


def _confidence_for_score(score: int) -> Confidence:
    if score >= 5:
        return "high"
    if score >= 2:
        return "medium"
    return "low"


def _candidate_urls(official_website: str) -> list[str]:
    if not official_website:
        return []
    parsed = urlparse(official_website)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    base_host = parsed.netloc.replace("www.", "")
    out: list[str] = []
    for path in PATH_PATTERNS:
        out.append(urljoin(origin, path))
    for host_pat in HOST_PATTERNS:
        host = host_pat.format(host=base_host)
        out.append(f"https://{host}/")
    # De-dupe preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for u in out:
        if u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped


def _harvest_links_from_sitemap(client, official_website: str) -> list[str]:
    """Fetch /sitemap.xml (and /sitemap_index.xml as fallback) and return
    URLs whose path contains an election-related term. Most CivicEngage,
    CivicPlus, Granicus, and Drupal-based gov sites publish sitemaps —
    this is the most reliable way to find pages whose URLs don't follow
    a standard /elections convention (e.g. CivicEngage's
    /{categoryID}/{Name} layout)."""
    import re as _re
    parsed = urlparse(official_website)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    sitemap_urls = [f"{origin}/sitemap.xml", f"{origin}/sitemap_index.xml"]
    discovered: list[str] = []
    for sitemap_url in sitemap_urls:
        try:
            r = client.get(sitemap_url)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        # Parse <loc>URL</loc> tags. Cheap regex is fine — sitemaps
        # are simple XML and ElementTree on real-world sitemaps with
        # mixed encoding/quirks is more trouble than it's worth.
        for m in _re.finditer(r"<loc>([^<]+)</loc>", r.text, _re.IGNORECASE):
            url = m.group(1).strip()
            url_lower = url.lower()
            # If this is a nested sitemap (sitemap index), recurse one level.
            if url_lower.endswith(".xml"):
                try:
                    rr = client.get(url)
                    if rr.status_code == 200:
                        for mm in _re.finditer(r"<loc>([^<]+)</loc>", rr.text, _re.IGNORECASE):
                            sub_url = mm.group(1).strip()
                            if any(kw in sub_url.lower() for kw in ("election", "vote", "ballot", "boe", "poll")):
                                discovered.append(sub_url)
                except Exception:
                    pass
                continue
            if any(kw in url_lower for kw in ("election", "vote", "ballot", "boe", "poll")):
                discovered.append(url)
        if discovered:
            break  # found something via the primary sitemap; don't also probe the index
    # De-dupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for u in discovered:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _harvest_links_from_homepage(client, homepage_url: str) -> list[str]:
    """Fetch the homepage and pull any <a href> whose URL path or
    visible text mentions election-related terms. Returns absolute URLs."""
    import re as _re
    try:
        r = client.get(homepage_url)
        if r.status_code != 200:
            return []
    except Exception:
        return []
    html = r.text
    # Find each <a ... href="..." ...>text</a>
    pattern = _re.compile(
        r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        _re.IGNORECASE | _re.DOTALL,
    )
    keep: list[str] = []
    for m in pattern.finditer(html):
        href = m.group(1).strip()
        text = _re.sub(r"<[^>]+>", "", m.group(2)).strip().lower()
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        # Match against URL path or visible link text
        signal = (href.lower() + " " + text)
        if any(kw in signal for kw in ("election", "vote", "ballot", "boe", "polls")):
            keep.append(urljoin(str(r.url), href))
    # De-dupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for u in keep:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def discover_one(slug: str) -> list[ElectionSourceProposal]:
    config = load_config(slug)
    official_website = config["jurisdiction"].get("official_website", "")
    if not official_website:
        print(f"  ⊘ {slug}: no official_website in config", file=sys.stderr)
        return []

    proposals: list[ElectionSourceProposal] = []

    with civic_client(default_timeout=15.0) as client:
        # Strategy 1: probe common URL patterns
        print(f"  → probing {len(PATH_PATTERNS) + len(HOST_PATTERNS)} URL patterns...", file=sys.stderr)
        for url in _candidate_urls(official_website):
            try:
                r = client.get(url)
            except Exception:
                continue
            if r.status_code != 200:
                continue
            if "text/html" not in (r.headers.get("content-type") or "").lower():
                continue
            score, matched = _score_page(r.text, str(r.url))
            if score >= 1:
                proposals.append(_proposal_from_response(r, score, matched, method="url_pattern_probe"))

        # Strategy 2: scan sitemap.xml for election-related URLs. Most
        # gov sites publish one, and it indexes every page — including
        # the ones at non-standard paths like CivicEngage's
        # /{categoryID}/{Name} layout that the pattern probe can't
        # anticipate.
        if not proposals or all(p.confidence == "low" for p in proposals):
            print("  → scanning sitemap.xml for elections URLs...", file=sys.stderr)
            sitemap_urls = _harvest_links_from_sitemap(client, official_website)
            print(f"     {len(sitemap_urls)} sitemap candidate(s)", file=sys.stderr)
            for url in sitemap_urls:
                try:
                    r = client.get(url)
                except Exception:
                    continue
                if r.status_code != 200:
                    continue
                if "text/html" not in (r.headers.get("content-type") or "").lower():
                    continue
                score, matched = _score_page(r.text, str(r.url))
                if score >= 1:
                    proposals.append(_proposal_from_response(r, score, matched, method="sitemap_scan"))

        # Strategy 3: harvest election-related links from the homepage
        # and score each. Last-resort because homepages often don't link
        # directly to internal department pages — fine for cities, weaker
        # for counties with deep menu structures.
        if not proposals or all(p.confidence == "low" for p in proposals):
            print("  → harvesting elections links from homepage...", file=sys.stderr)
            for url in _harvest_links_from_homepage(client, official_website):
                try:
                    r = client.get(url)
                except Exception:
                    continue
                if r.status_code != 200:
                    continue
                if "text/html" not in (r.headers.get("content-type") or "").lower():
                    continue
                score, matched = _score_page(r.text, str(r.url))
                if score >= 1:
                    proposals.append(_proposal_from_response(r, score, matched, method="homepage_link_harvest"))

    # De-dupe by URL keeping best raw score (not just confidence tier —
    # the tier is just a label, and the score carries the URL-shape
    # bonuses that decide ties within a tier).
    by_url: dict[str, ElectionSourceProposal] = {}
    for p in proposals:
        existing = by_url.get(p.url)
        if existing is None or p.score > existing.score:
            by_url[p.url] = p
    return sorted(by_url.values(), key=lambda p: -p.score)


def _proposal_from_response(r, score, matched, *, method):
    import re as _re
    title_match = _re.search(r"<title[^>]*>(.*?)</title>", r.text, _re.DOTALL | _re.IGNORECASE)
    title = (title_match.group(1).strip() if title_match else "")[:200]
    return ElectionSourceProposal(
        url=str(r.url),
        confidence=_confidence_for_score(score),
        method=method,
        score=score,
        title=title,
        matched_keywords=matched,
    )


def _rank(c: Confidence) -> int:
    return {"high": 3, "medium": 2, "low": 1}[c]


def apply_to_config(slug: str, proposal: ElectionSourceProposal) -> None:
    path = JURISDICTIONS_DIR / f"{slug}.json"
    cfg = json.loads(path.read_text())
    existing_conf = (cfg.get("elections") or {}).get("results_endpoint_confidence")
    if existing_conf == "high" and proposal.confidence != "high":
        print(f"  ⊘ {slug}: existing high-confidence URL — refusing overwrite with {proposal.confidence}", file=sys.stderr)
        return
    cfg.setdefault("elections", {})
    cfg["elections"]["results_endpoint"] = proposal.url
    cfg["elections"]["results_endpoint_confidence"] = proposal.confidence
    # Format is "html" for landing pages discovered this way; the ingest
    # job decides whether to drill into PDFs or render to vision based on
    # what's actually at the URL.
    cfg["elections"].setdefault("results_format", "html")
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"  ✓ {slug}: applied (confidence={proposal.confidence}, method={proposal.method})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", help="Only this jurisdiction. Default: all configs.")
    parser.add_argument("--apply", action="store_true", help="Write proposed URL back into the config file.")
    args = parser.parse_args()

    slugs = [args.slug] if args.slug else list_slugs()
    for slug in slugs:
        print(f"\n=== {slug} ===", file=sys.stderr)
        proposals = discover_one(slug)
        if not proposals:
            print(f"  ✗ {slug}: no candidates found", file=sys.stderr)
            continue
        best = proposals[0]
        print(f"\n  best: {best.confidence}  via {best.method}")
        print(f"    title:    {best.title}")
        print(f"    url:      {best.url}")
        print(f"    matched:  {', '.join(best.matched_keywords)}")
        if len(proposals) > 1:
            print(f"\n  other candidates ({len(proposals) - 1}):")
            for c in proposals[1:6]:
                print(f"    [{c.confidence}] {c.url}  ({', '.join(c.matched_keywords[:3])})")
        if args.apply:
            apply_to_config(slug, best)
    return 0


if __name__ == "__main__":
    sys.exit(main())
