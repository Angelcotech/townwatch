-- Migration 056: onboarding bundles in jurisdiction_directory.
--
-- Onboarding is bundled per general-purpose government so automation never
-- splits a citizen's "town" into separately-funded pieces: a county school
-- system onboards with its county; an independent city school system onboards
-- with its city; a consolidated city-county place row onboards with its county
-- (one government, never two onboardings). bundle_fips points at the directory
-- entry this row rides along with: 5-digit = county, 7-digit = place. NULL =
-- this row is its own onboarding unit. Derived automatically by
-- seed_jurisdiction_directory from Census naming conventions; see
-- research/ga_recon/UNIVERSE_SOURCES.md for the verified GA universe.

ALTER TABLE jurisdiction_directory
    ADD COLUMN IF NOT EXISTS bundle_fips TEXT;

CREATE INDEX IF NOT EXISTS jurisdiction_directory_bundle_idx
    ON jurisdiction_directory (state_abbr, bundle_fips)
    WHERE bundle_fips IS NOT NULL;
