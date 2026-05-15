"""
Photo scraper base — what every platform-specific scraper must implement.

A scraper takes the jurisdiction's council/officials page URL and returns
zero or more PhotoCandidate objects. The downstream verification + storage
logic is the same for all platforms.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class PhotoCandidate:
    """One photo found on a public page, with full provenance + enrichment."""
    # Who the photo is supposedly of (the name as displayed on the source page)
    source_name: str

    # Their seat/title as displayed (e.g., "Mayor Pro Tem", "Councilmember")
    source_title: str | None

    # Direct URL to the image
    photo_url: str

    # URL of the page that displays the photo
    source_url: str

    # Tier of the source: city_official > state_official > press > social > other
    source_tier: str

    # Alt text or nearby caption — used in verification scoring
    caption: str | None

    # Platform tag — civicengage, granicus, wordpress, vision_fallback, ...
    platform: str

    # Biographical text scraped from the same page (multi-sentence narrative)
    bio_text: str | None = None


class PhotoScraper(ABC):
    """Abstract scraper. One subclass per platform we know how to read."""

    platform: str = ""

    @abstractmethod
    def scrape(self, council_url: str, jurisdiction_domain: str) -> list[PhotoCandidate]:
        """
        Fetch the council/officials page and return every photo we can find.

        jurisdiction_domain — used to tag the source_tier (city_official iff
        the photo URL is on the jurisdiction's own domain).
        """
