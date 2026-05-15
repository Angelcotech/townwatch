"""
Photo-scraper dispatcher.

Routes to the right platform-specific scraper based on the jurisdiction's
declared agenda_platform. When a new platform appears (Granicus, BoardDocs,
WordPress, etc.), add a module here and register it in the REGISTRY.

The vision_fallback scraper is the universal "any-site" option for towns
whose platform we don't yet recognize — slower per use but zero new code
needed to onboard.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import PhotoScraper


def get_scraper(platform: str) -> "PhotoScraper":
    """
    Return the photo scraper for a platform key (from jurisdiction.platform_hints).
    Raises ValueError if no scraper is registered AND vision_fallback isn't loadable.
    """
    from .civicengage import CivicEngagePhotoScraper

    REGISTRY = {
        "civicengage": CivicEngagePhotoScraper,
        # "granicus": GranicusPhotoScraper,   # add when implemented
        # "wordpress": WordPressPhotoScraper,
    }

    cls = REGISTRY.get(platform)
    if cls is None:
        raise ValueError(
            f"No photo scraper registered for platform '{platform}'. "
            f"Available: {list(REGISTRY)}. Add a scraper or set platform "
            f"to 'vision_fallback' once that module is implemented."
        )
    return cls()
