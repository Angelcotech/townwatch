-- Migration 057: nav fields on jurisdiction_directory (county_fips + slug).
--
-- The public site is moving to a single finder: a State → County → Town cascade
-- listing EVERY jurisdiction (covered or not), and every jurisdiction gets its own
-- /[state]/[slug] page. Two fields the directory lacks for that:
--
--  * county_fips — which county a row groups under in the cascade. The Census place
--    gazetteer (seed_jurisdiction_directory) doesn't carry a city's county, and
--    bundle_fips is for ONBOARDING bundling (NULL for ordinary cities), not nav. So
--    this is a separate nav-grouping column. (counties → self; school districts /
--    consolidated → their county via bundle_fips; ordinary cities → from the recon
--    universe roster's place→county mapping.)
--  * slug — stable URL key for uncovered rows (covered rows already resolve via
--    jurisdiction.slug; this mirrors it so the URL is stable across onboarding).
--
-- Backfilled by jobs/backfill_directory_nav.py (reads research/ga_recon/
-- universe_roster.json + bundle_fips). NOT unique on slug: consolidated governments
-- appear as both a county row and a consolidated-city row that slugify the same
-- (Echols/Webster) — the city row is excluded from the cascade and routing prefers
-- the county row.

ALTER TABLE jurisdiction_directory ADD COLUMN IF NOT EXISTS county_fips TEXT;
ALTER TABLE jurisdiction_directory ADD COLUMN IF NOT EXISTS slug TEXT;

CREATE INDEX IF NOT EXISTS jurisdiction_directory_slug_idx
    ON jurisdiction_directory (state_abbr, slug) WHERE slug IS NOT NULL;
CREATE INDEX IF NOT EXISTS jurisdiction_directory_county_idx
    ON jurisdiction_directory (state_abbr, county_fips) WHERE county_fips IS NOT NULL;
