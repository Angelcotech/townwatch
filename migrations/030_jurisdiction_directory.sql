-- Migration 030: jurisdiction_directory — the "map broadly, activate on demand"
-- catalog.
--
-- The jurisdiction table holds ONBOARDED governments (those with indexed
-- bodies/meetings/records). This directory is the full searchable universe of
-- governments — seeded from the Census gazetteer — so a visitor can look up
-- their own city/county and learn whether it's covered yet, and adopt it if
-- not. Covered entries point back at their jurisdiction row; the rest are
-- uncovered stubs (the funding funnel).
--
-- Scoped to Georgia for now; the same table scales to all 50 states when we
-- seed them (one row per city/county, keyed by Census FIPS/GEOID).

CREATE TABLE IF NOT EXISTS jurisdiction_directory (
    id                       BIGSERIAL PRIMARY KEY,
    fips                     TEXT NOT NULL,           -- Census GEOID (7-digit place / 5-digit county)
    name                     TEXT NOT NULL,           -- display name: "Atlanta", "Columbia County"
    jurisdiction_type        TEXT NOT NULL CHECK (jurisdiction_type IN ('city', 'county')),
    state_abbr               TEXT NOT NULL,
    -- Set when this directory entry has been onboarded; NULL = uncovered stub.
    covered_jurisdiction_id  INTEGER REFERENCES jurisdiction (id),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (state_abbr, jurisdiction_type, fips)
);

CREATE INDEX IF NOT EXISTS jurisdiction_directory_search_idx
    ON jurisdiction_directory (state_abbr, name);
CREATE INDEX IF NOT EXISTS jurisdiction_directory_covered_idx
    ON jurisdiction_directory (covered_jurisdiction_id);
