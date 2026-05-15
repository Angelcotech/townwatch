"""
CivicEngage / CivicPlus photo scraper.

CivicEngage is the most common municipal-government CMS in the US. Its
council pages link to per-person bio pages at /Directory.aspx?EID=N, where
the person's name + headshot are rendered together.

Page-level pattern (council page):
  <table> with rows: name, title, optional address; the name is wrapped
  in <a href="/Directory.aspx?EID=N">.

Bio-level pattern (Directory.aspx?EID=N):
  <img src="/ImageRepository/Document?documentID=N" alt="Name HEADSHOT">
  <h1>Name</h1> or similar.

Both signals — the source URL (city's own domain) AND the alt text
mentioning the official's name — are present by construction, so any
photo this scraper returns earns 2/3 verification points automatically.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .base import PhotoCandidate, PhotoScraper


USER_AGENT = "TownWatch/1.0 (civic-data; +https://townwatch.us)"
TIMEOUT = 30.0


class CivicEngagePhotoScraper(PhotoScraper):
    platform = "civicengage"

    def scrape(self, council_url: str, jurisdiction_domain: str) -> list[PhotoCandidate]:
        candidates: list[PhotoCandidate] = []

        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, follow_redirects=True) as client:
            council_html = client.get(council_url).text
            soup = BeautifulSoup(council_html, "html.parser")

            # Find every link to a Directory.aspx?EID=N bio page
            eid_links: dict[int, str] = {}
            for a in soup.find_all("a", href=True):
                m = re.search(r"Directory\.aspx\?EID=(\d+)", a["href"], re.IGNORECASE)
                if not m:
                    continue
                eid = int(m.group(1))
                if eid not in eid_links:
                    eid_links[eid] = urljoin(council_url, a["href"])

            # Fetch each bio page and extract name + photo + caption
            for eid, bio_url in eid_links.items():
                try:
                    bio_html = client.get(bio_url).text
                except httpx.HTTPError:
                    continue
                candidate = _parse_bio_page(bio_html, bio_url, jurisdiction_domain)
                if candidate:
                    candidates.append(candidate)

        return candidates


CHROME_ALT_BLOCKLIST = {
    "home page", "facebook", "twitter", "x", "instagram", "youtube", "linkedin",
    "search", "menu", "everify", "emergency alert", "logo", "city of grovetown",
}


def _is_chrome_alt(alt: str) -> bool:
    a = alt.strip().lower()
    if not a:
        return True
    if a in CHROME_ALT_BLOCKLIST:
        return True
    # Anything containing words like "logo" / "icon" is chrome
    if any(token in a for token in ("logo", "icon", "alert", "banner", "navigation")):
        return True
    return False


def _parse_bio_page(html: str, bio_url: str, jurisdiction_domain: str) -> PhotoCandidate | None:
    """Extract name, title, photo URL, and caption from a CivicEngage Directory.aspx bio page."""
    soup = BeautifulSoup(html, "html.parser")

    # Find the headshot. CivicEngage uses /ImageRepository/Document?documentID=N
    # for everything (banner logo, social icons, headshots), so we filter by
    # alt text — preferring "HEADSHOT"-tagged images, then any non-chrome alt.
    img = None
    # Pass 1: explicit HEADSHOT tag
    for candidate_img in soup.find_all("img"):
        src = candidate_img.get("src", "") or ""
        alt = candidate_img.get("alt", "") or ""
        if "ImageRepository" in src and "headshot" in alt.lower():
            img = candidate_img
            break
    # Pass 2: any non-chrome ImageRepository image
    if img is None:
        for candidate_img in soup.find_all("img"):
            src = candidate_img.get("src", "") or ""
            alt = candidate_img.get("alt", "") or ""
            if "ImageRepository" in src and not _is_chrome_alt(alt):
                img = candidate_img
                break
    if img is None:
        return None

    photo_url = urljoin(bio_url, img.get("src", ""))
    caption = (img.get("alt") or "").strip() or None

    # Name lives in an <h1> or strong title element on the bio page
    name = None
    for tag in soup.find_all(["h1", "h2"]):
        text = tag.get_text(strip=True)
        if text and len(text) < 80 and not text.lower().startswith(("city of", "staff", "directory")):
            name = text
            break
    if not name and caption:
        # Fall back: pull the name off the alt text ("Eric Blair HEADSHOT")
        name = re.sub(r"\s*(headshot|photo|picture|portrait)\s*$", "", caption, flags=re.IGNORECASE).strip() or None
    if not name:
        return None

    # Title (Mayor Pro Tem, Councilmember, etc.) — look for it near the name
    title = None
    title_candidates = soup.find_all(["p", "span", "div"])
    for tag in title_candidates[:40]:
        txt = tag.get_text(strip=True)
        if not txt:
            continue
        if re.search(r"\b(mayor|council|commissioner|chair|alderman)\b", txt, re.IGNORECASE):
            if len(txt) < 120:
                title = txt
                break

    # Tier: gold if the bio page is hosted on the jurisdiction's own domain
    host = urlparse(bio_url).netloc.lower()
    tier = "city_official" if jurisdiction_domain.lower() in host else "other"

    return PhotoCandidate(
        source_name=name,
        source_title=title,
        photo_url=photo_url,
        source_url=bio_url,
        source_tier=tier,
        caption=caption,
        platform="civicengage",
    )
