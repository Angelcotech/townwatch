-- Migration 052: canonical jurisdiction.slug
--
-- Jurisdiction identity was derived three different ways: the ETL joins on
-- fips_code, the config/CLI uses a slug ('grovetown-ga'), and the web DERIVED a
-- URL slug from the mutable display name — LOWER(REPLACE(name,' ','-')) — in ~10
-- places, with two inconsistent conventions. A rename would silently break URLs,
-- and at scale (duplicate city names, punctuation) the derivation is unsafe.
--
-- This stores ONE immutable, city-level slug. Identity is the (state_abbr, slug)
-- pair, which mirrors the /[state]/[city] route exactly. Backfilled to the value
-- the web derives today, so NO existing URL changes — we just stop deriving it
-- from a mutable field. sync_jurisdictions keeps it set from the config handle.

ALTER TABLE jurisdiction ADD COLUMN IF NOT EXISTS slug TEXT;

-- Backfill = exactly what getJurisdictionBySlug matched on, so every current
-- route resolves unchanged.
UPDATE jurisdiction SET slug = LOWER(REPLACE(name, ' ', '-')) WHERE slug IS NULL;

ALTER TABLE jurisdiction ALTER COLUMN slug SET NOT NULL;

-- Globally unique per state — the human-facing composite key behind the URL.
CREATE UNIQUE INDEX IF NOT EXISTS uq_jurisdiction_state_slug
    ON jurisdiction (LOWER(state_abbr), slug);
