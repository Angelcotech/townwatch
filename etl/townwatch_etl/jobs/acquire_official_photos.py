"""
Acquire + verify photos for every official in a jurisdiction.

Pipeline:
  1. Read jurisdiction config → council page URL + agenda_platform key
  2. Dispatch to platform-specific scraper → list[PhotoCandidate]
  3. For each candidate:
       a. Identity-resolve source_name → official_id (CachedResolver)
       b. Download the photo bytes (cache locally so we don't depend on the source URL)
       c. Score verification:
            +1  source_tier='city_official' (URL is jurisdiction's own domain)
            +1  caption contains the official's canonical_name (case-insensitive)
            +1  (future) Sonnet vision confirms identity
       d. data_status='verified' iff score >= 2, else 'unverified'
  4. Insert into official_photo with full provenance

A photo only appears on public surfaces (Streamlit profile pages, roster
cards) when data_status='verified'. Unverified candidates sit in the
operator queue.

Run:
    python -m townwatch_etl.jobs.acquire_official_photos --jurisdiction grovetown-ga
    python -m townwatch_etl.jobs.acquire_official_photos --jurisdiction grovetown-ga --force
"""

from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import urlparse

import httpx
import psycopg

from ..ingest_base import IngestJob
from ..identity import CachedResolver
from ..jurisdiction import load_config
from ..scrapers.photos import get_scraper
from ..scrapers.photos.base import PhotoCandidate


USER_AGENT = "TownWatch/1.0 (civic-data; +https://townwatch.us)"


class AcquireOfficialPhotos(IngestJob):
    source_name = "official_photos_scraper"
    source_type = "scrape"
    source_url = "internal://photos"

    def __init__(self, *, jurisdiction: str, force: bool = False):
        super().__init__()
        self.jurisdiction_slug = jurisdiction
        self.force = force
        self.summary: dict | None = None

    def ingest(self) -> None:
        assert self.conn is not None
        cfg = load_config(self.jurisdiction_slug)

        # The jurisdiction in DB
        jur_row = self.conn.execute(
            "SELECT id, fips_code FROM jurisdiction WHERE fips_code = %s",
            (cfg["jurisdiction"]["place_fips"],),
        ).fetchone()
        if not jur_row:
            raise RuntimeError(
                f"Jurisdiction {self.jurisdiction_slug} not in DB. Run civicengage_officials first."
            )
        jurisdiction_id = jur_row["id"]
        official_website = cfg["jurisdiction"]["official_website"]
        domain = urlparse(official_website).netloc.lower()

        # Roster source URL — typically the council page
        roster = cfg["data_sources"]["officials_roster"]
        council_url = roster["url"]
        platform = cfg["platform_hints"]["agenda_platform"]

        print(f"  · platform={platform}")
        print(f"  · council_url={council_url}")
        print(f"  · jurisdiction_domain={domain}")

        # Run the platform-specific scraper
        scraper = get_scraper(platform)
        candidates = scraper.scrape(council_url, jurisdiction_domain=domain)
        print(f"\n  scraper returned {len(candidates)} candidate(s)")

        resolver = CachedResolver(
            self.conn,
            jurisdiction_id=jurisdiction_id,
            data_source_id=self.data_source_id,
            source_system="photo_scraper",
        )

        verified_count = 0
        unverified_count = 0
        unresolved_count = 0
        skipped_count = 0

        for c in candidates:
            print(f"\n  → {c.source_name}")
            official_id = resolver.resolve(c.source_name)
            if official_id is None:
                print(f"     ⨯ could not resolve to any official; skipping")
                unresolved_count += 1
                continue

            # Look up canonical_name for scoring
            row = self.conn.execute(
                "SELECT canonical_name FROM official WHERE id = %s", (official_id,)
            ).fetchone()
            canonical = row["canonical_name"]
            print(f"     resolved → official #{official_id} ({canonical})")

            # Enrich: write display_title + bio_text from the same scraped page.
            # We always overwrite (the city's page is the source of truth).
            updates = []
            params = []
            if c.source_title:
                updates.append("display_title = %s")
                params.append(c.source_title)
            if c.bio_text:
                updates.append("bio_text = %s")
                params.append(c.bio_text)
            if updates:
                params.append(official_id)
                self.conn.execute(
                    f"UPDATE official SET {', '.join(updates)} WHERE id = %s",
                    tuple(params),
                )
                bits = []
                if c.source_title:
                    bits.append(f"title={c.source_title!r}")
                if c.bio_text:
                    bits.append(f"bio={len(c.bio_text)}ch")
                print(f"     enriched ({', '.join(bits)})")

            # Idempotency: if we already have THIS photo_url, skip unless --force
            existing = self.conn.execute(
                "SELECT id, data_status FROM official_photo WHERE official_id = %s AND photo_url = %s",
                (official_id, c.photo_url),
            ).fetchone()
            if existing and not self.force:
                print(f"     ⊘ already in DB (status={existing['data_status']}); skipping (use --force to refresh)")
                skipped_count += 1
                continue

            # Download photo bytes
            try:
                pic = _download_image(c.photo_url)
            except Exception as e:
                print(f"     ! photo download failed: {e}")
                continue

            # Verification scoring
            score = 0
            reasons = []
            if c.source_tier == "city_official":
                score += 1
                reasons.append("source=city_official")
            if c.caption and canonical.lower() in c.caption.lower():
                score += 1
                reasons.append("caption_matches_canonical_name")

            data_status = "verified" if score >= 2 else "unverified"
            verified_by = f"auto:{'+'.join(reasons)}" if data_status == "verified" else None

            if existing:
                self.conn.execute("""
                    UPDATE official_photo
                    SET photo_bytes = %s, photo_mime = %s, source_url = %s,
                        source_tier = %s, source_caption = %s, source_platform = %s,
                        verification_score = %s, data_status = %s,
                        verified_at = CASE WHEN %s = 'verified' THEN NOW() ELSE NULL END,
                        verified_by = %s, updated_at = NOW()
                    WHERE id = %s
                """, (
                    pic["bytes"], pic["mime"], c.source_url,
                    c.source_tier, c.caption, c.platform,
                    score, data_status, data_status, verified_by,
                    existing["id"],
                ))
            else:
                self.conn.execute("""
                    INSERT INTO official_photo
                      (official_id, photo_url, photo_bytes, photo_mime,
                       source_url, source_tier, source_caption, source_platform,
                       verification_score, data_status,
                       verified_at, verified_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            CASE WHEN %s = 'verified' THEN NOW() ELSE NULL END, %s)
                """, (
                    official_id, c.photo_url, pic["bytes"], pic["mime"],
                    c.source_url, c.source_tier, c.caption, c.platform,
                    score, data_status, data_status, verified_by,
                ))

            self.rows_written += 1
            marker = "✓ verified" if data_status == "verified" else "⚠ unverified"
            print(f"     {marker} (score={score}, reasons={reasons}, {pic['size']} bytes)")
            if data_status == "verified":
                verified_count += 1
            else:
                unverified_count += 1

        self.summary = {
            "verified": verified_count,
            "unverified": unverified_count,
            "unresolved": unresolved_count,
            "skipped_existing": skipped_count,
            "total_candidates": len(candidates),
        }
        print(f"\n  summary: {self.summary}")


def _download_image(url: str) -> dict:
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0, follow_redirects=True) as c:
        r = c.get(url)
        r.raise_for_status()
    content_type = r.headers.get("content-type", "image/jpeg").split(";", 1)[0].strip()
    return {"bytes": r.content, "mime": content_type, "size": len(r.content)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jurisdiction", required=True, help="Slug (e.g. grovetown-ga)")
    parser.add_argument("--force", action="store_true", help="Re-fetch photos even if already in DB")
    args = parser.parse_args()

    job = AcquireOfficialPhotos(jurisdiction=args.jurisdiction, force=args.force)
    result = job.run()
    print()
    print(json.dumps({"job": result, "photos": job.summary}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
