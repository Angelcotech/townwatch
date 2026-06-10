-- Migration 055: jurisdiction_directory grows a school_district layer.
--
-- The searchable directory was seeded with cities + counties only; school
-- systems are their own governments (GA: 159 county + 21 independent city
-- systems = 180 regular LEAs per NCES CCD — verified in
-- research/ga_recon/UNIVERSE_SOURCES.md) and belong in the "find your town"
-- universe. fips for school districts is the Census unified-school-district
-- GEOID (state + 5-digit SDLEA, e.g. 1301410 = Columbia County School
-- District), matching jurisdiction.fips_code for onboarded districts.

ALTER TABLE jurisdiction_directory
    DROP CONSTRAINT IF EXISTS jurisdiction_directory_jurisdiction_type_check;
ALTER TABLE jurisdiction_directory
    ADD CONSTRAINT jurisdiction_directory_jurisdiction_type_check
    CHECK (jurisdiction_type IN ('city', 'county', 'school_district'));
