"""
Acquire non-elected staff officials (City Administrator, dept directors,
attorney, etc.) from the city's CivicEngage Staff Directory.

Same pipeline as acquire_official_photos, but with one critical
difference: when a name doesn't resolve to an existing official, we
CREATE a new record with is_elected=False rather than skipping.

This populates the platform with profile pages for the staff who shape
most council decisions — Bradley Smith (Finance Director, 14+ motions
recommended), John Waller (former City Administrator, 17+), and so on.

The qa_orphan_official detector was updated in tandem to skip records
with is_elected=False, so newly-created staff don't get auto-deleted.

Run:
    python -m townwatch_etl.jobs.acquire_staff --jurisdiction grovetown-ga
    python -m townwatch_etl.jobs.acquire_staff --jurisdiction grovetown-ga --force
"""

from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import urlparse

from ..http_client import civic_get

from ..ingest_base import IngestJob
from ..identity import CachedResolver, add_alias, create_official
from ..jurisdiction import load_config
from ..scrapers.photos.civicengage import CivicEngagePhotoScraper

USER_AGENT = "TownWatch/1.0 (civic-data; +https://townwatch.us)"


def _split_name(full: str) -> tuple[str | None, str]:
    parts = full.strip().split()
    if not parts:
        return None, ""
    if len(parts) == 1:
        return None, parts[0]
    return " ".join(parts[:-1]), parts[-1]


def _download_image(url: str) -> dict:
    r = civic_get(url, timeout=30.0)
    r.raise_for_status()
    content_type = r.headers.get("content-type", "image/jpeg").split(";", 1)[0].strip()
    return {"bytes": r.content, "mime": content_type, "size": len(r.content)}


class AcquireStaff(IngestJob):
    source_name = "civicengage_staff_directory"
    source_type = "scrape"
    source_url = "internal://staff"

    def __init__(self, *, jurisdiction: str, force: bool = False):
        super().__init__()
        self.jurisdiction_slug = jurisdiction
        self.force = force
        self.summary: dict | None = None

    def ingest(self) -> None:
        assert self.conn is not None and self.data_source_id is not None
        cfg = load_config(self.jurisdiction_slug)

        jur_row = self.conn.execute(
            "SELECT id FROM jurisdiction WHERE fips_code = %s",
            (cfg["jurisdiction"]["place_fips"],),
        ).fetchone()
        if not jur_row:
            raise RuntimeError(
                f"Jurisdiction {self.jurisdiction_slug} not in DB. Run civicengage_officials first."
            )
        jurisdiction_id = jur_row["id"]
        official_website = cfg["jurisdiction"]["official_website"]
        domain = urlparse(official_website).netloc.lower()

        # The Staff Directory index lives at /Directory.aspx on every
        # CivicEngage city site. We make this a platform-level convention
        # rather than a per-jurisdiction config field.
        directory_index_url = f"{official_website.rstrip('/')}/Directory.aspx"
        print(f"  · directory_index_url={directory_index_url}")

        scraper = CivicEngagePhotoScraper()
        candidates = scraper.scrape_staff(directory_index_url, jurisdiction_domain=domain)
        print(f"\n  scraper returned {len(candidates)} candidate(s)")

        resolver = CachedResolver(
            self.conn,
            jurisdiction_id=jurisdiction_id,
            data_source_id=self.data_source_id,
            source_system="staff_scraper",
        )

        created_count = 0
        enriched_count = 0
        verified_count = 0
        unverified_count = 0
        skipped_count = 0
        seen_official_ids: set[int] = set()

        for c in candidates:
            print(f"\n  → {c.source_name} ({c.source_title or 'no title'})")

            # Resolve or create. We try several name forms; CivicEngage
            # shows just "First Last", but motions may have captured them
            # as "Title First Last" (e.g., "City Administrator Elaine Matthews").
            official_id = resolver.resolve(c.source_name)

            if official_id is None:
                first, last = _split_name(c.source_name)
                if not last:
                    print(f"     ⨯ could not parse name; skipping")
                    skipped_count += 1
                    continue
                official_id = create_official(
                    self.conn,
                    data_source_id=self.data_source_id,
                    canonical_name=c.source_name,
                    first_name=first,
                    last_name=last,
                    bio_text=c.bio_text,
                )
                # Mark as non-elected
                self.conn.execute(
                    "UPDATE official SET is_elected = FALSE, display_title = %s WHERE id = %s",
                    (c.source_title, official_id),
                )
                # Add a primary alias so this name resolves on future runs
                add_alias(
                    self.conn,
                    official_id=official_id,
                    alias_name=c.source_name,
                    source_system="staff_scraper",
                    data_source_id=self.data_source_id,
                )
                # Also alias by "Title FirstName LastName" because that's
                # how the LLM extractor captured staff in past motions.
                if c.source_title:
                    title_form = f"{c.source_title} {c.source_name}"
                    add_alias(
                        self.conn,
                        official_id=official_id,
                        alias_name=title_form,
                        source_system="staff_scraper",
                        data_source_id=self.data_source_id,
                    )
                resolver.register_new_official(
                    official_id, canonical_name=c.source_name, last_name=last
                )
                # Newly-scraped person from the live directory is by
                # definition active.
                self.conn.execute(
                    "UPDATE official SET is_active = TRUE WHERE id = %s",
                    (official_id,),
                )
                created_count += 1
                print(f"     + created staff official #{official_id} (is_elected=false, is_active=true)")
            else:
                # Existing record — enrich title + bio. DO NOT touch
                # is_elected: this job runs on the full staff directory
                # which includes the elected council, so blindly flipping
                # is_elected=FALSE would mis-classify them as staff.
                # DO mark is_active=TRUE: they're on the current directory.
                updates = ["is_active = TRUE"]
                params: list = []
                if c.source_title:
                    updates.append("display_title = %s")
                    params.append(c.source_title)
                if c.bio_text:
                    updates.append("bio_text = %s")
                    params.append(c.bio_text)
                params.append(official_id)
                self.conn.execute(
                    f"UPDATE official SET {', '.join(updates)} WHERE id = %s",
                    tuple(params),
                )
                enriched_count += 1
                print(f"     enriched existing official #{official_id} (is_active=true)")

            seen_official_ids.add(official_id)

            # Look up canonical name for photo verification scoring
            row = self.conn.execute(
                "SELECT canonical_name FROM official WHERE id = %s", (official_id,)
            ).fetchone()
            canonical = row["canonical_name"]

            existing_photo = self.conn.execute(
                "SELECT id, data_status FROM official_photo WHERE official_id = %s AND photo_url = %s",
                (official_id, c.photo_url),
            ).fetchone()
            if existing_photo and not self.force:
                skipped_count += 1
                continue

            try:
                pic = _download_image(c.photo_url)
            except Exception as e:
                print(f"     ! photo download failed: {e}")
                continue

            score = 0
            reasons = []
            if c.source_tier == "city_official":
                score += 1
                reasons.append("source=city_official")
            cap = (c.caption or "").lower()
            last = (canonical.split()[-1] if canonical else "").lower()
            if cap and canonical.lower() in cap:
                score += 1
                reasons.append("caption_matches_canonical_name")
            elif cap and last and last in cap and len(last) >= 4:
                # Nicknames are common on staff bios — Chris vs Christopher,
                # Sonny vs Santino. Surname match on a gold-tier source is
                # a strong-enough secondary signal.
                score += 1
                reasons.append("caption_matches_last_name")
            data_status = "verified" if score >= 2 else "unverified"
            verified_by = f"auto:{'+'.join(reasons)}" if data_status == "verified" else None

            if existing_photo:
                self.conn.execute("""
                    UPDATE official_photo
                    SET photo_bytes = %s, photo_mime = %s, source_url = %s,
                        source_tier = %s, source_caption = %s, source_platform = %s,
                        verification_score = %s, data_status = %s,
                        verified_at = CASE WHEN %s = 'verified' THEN NOW() ELSE NULL END,
                        verified_by = %s, updated_at = NOW()
                    WHERE id = %s
                """, (pic["bytes"], pic["mime"], c.source_url, c.source_tier, c.caption,
                      c.platform, score, data_status, data_status, verified_by, existing_photo["id"]))
            else:
                self.conn.execute("""
                    INSERT INTO official_photo
                      (official_id, photo_url, photo_bytes, photo_mime,
                       source_url, source_tier, source_caption, source_platform,
                       verification_score, data_status, verified_at, verified_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            CASE WHEN %s = 'verified' THEN NOW() ELSE NULL END, %s)
                """, (official_id, c.photo_url, pic["bytes"], pic["mime"],
                      c.source_url, c.source_tier, c.caption, c.platform,
                      score, data_status, data_status, verified_by))

            self.rows_written += 1
            if data_status == "verified":
                verified_count += 1
            else:
                unverified_count += 1
            marker = "✓ verified" if data_status == "verified" else "⚠ unverified"
            print(f"     {marker} photo ({pic['size']} bytes)")

        # Mark staff who didn't appear in THIS scrape as inactive — they've
        # left the city / aren't on the directory anymore. Scoped to staff
        # only (is_elected=FALSE) so we don't touch elected officials whose
        # status is governed by term.is_current.
        if seen_official_ids:
            ids_tuple = tuple(seen_official_ids)
            placeholders = ",".join(["%s"] * len(ids_tuple))
            now_inactive = self.conn.execute(
                f"""UPDATE official SET is_active = FALSE
                    WHERE is_elected = FALSE AND is_active = TRUE
                      AND id NOT IN ({placeholders})
                    RETURNING id, canonical_name""",
                ids_tuple,
            ).fetchall()
            if now_inactive:
                print(f"\n  marked {len(now_inactive)} staff as inactive (no longer on directory):")
                for r in now_inactive:
                    print(f"    - #{r['id']} {r['canonical_name']}")

        self.summary = {
            "created": created_count,
            "enriched": enriched_count,
            "verified_photos": verified_count,
            "unverified_photos": unverified_count,
            "skipped": skipped_count,
            "total_candidates": len(candidates),
        }
        print(f"\n  summary: {self.summary}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jurisdiction", required=True, help="Slug (e.g. grovetown-ga)")
    parser.add_argument("--force", action="store_true", help="Re-fetch even if photos already cached")
    args = parser.parse_args()

    job = AcquireStaff(jurisdiction=args.jurisdiction, force=args.force)
    result = job.run()
    print()
    print(json.dumps({"job": result, "staff": job.summary}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
