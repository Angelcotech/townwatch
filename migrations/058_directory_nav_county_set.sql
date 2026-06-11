-- Migration 058: nav_county_fips — the full set of counties a town belongs to.
--
-- county_fips (057) is a single primary county, fine for display ("in X County").
-- But ~10% of GA cities span multiple counties (Atlanta → DeKalb + Fulton), and the
-- roster's county array is NOT population-ordered, so a single primary can put a city
-- under the wrong county in the cascade (Atlanta filed under DeKalb, missing from
-- Fulton). For the FINDER we want the city listed under every county it touches, so a
-- resident finds it under whichever county they think of. This stores that full set
-- (from the recon roster's municipalities[].counties); the cascade joins on it.
--
-- (A population-share primary for county_fips is a later refinement — it needs the
-- Census sub-county part-population file and belongs in seed_jurisdiction_directory,
-- which already parses the national Census files. This array keeps the public finder
-- correct in the meantime without coupling to that.)

ALTER TABLE jurisdiction_directory ADD COLUMN IF NOT EXISTS nav_county_fips TEXT[];

CREATE INDEX IF NOT EXISTS jurisdiction_directory_navcounty_idx
    ON jurisdiction_directory USING GIN (nav_county_fips);
