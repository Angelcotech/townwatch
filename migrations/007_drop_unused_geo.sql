-- Migration 007: drop unused geo columns
--
-- These were placeholders from the original PostGIS plan. We decided to
-- handle geocoding via the Census Geocoder API (address → FIPS) instead
-- of storing boundary polygons in Postgres, so these columns are dead
-- weight that invites confusion about whether they're load-bearing.
--
-- Verified empty before drop: all three contained 0 non-null rows.

ALTER TABLE jurisdiction DROP COLUMN IF EXISTS boundary;
ALTER TABLE jurisdiction DROP COLUMN IF EXISTS tiger_vintage_year;
ALTER TABLE seat         DROP COLUMN IF EXISTS district_boundary;
