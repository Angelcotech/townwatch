-- Migration 018: extend official_photo.source_tier enum to include county_official.
-- The original enum was city-scoped; counties have their own
-- equivalent-trust publication tier (the county's official directory page,
-- e.g. columbiacountyga.gov/304/Board-of-Commissioners). Same trust level
-- as city_official — sourced directly from the jurisdiction's own website.

ALTER TABLE official_photo
    DROP CONSTRAINT official_photo_source_tier_check;

ALTER TABLE official_photo
    ADD CONSTRAINT official_photo_source_tier_check
    CHECK (source_tier = ANY (ARRAY[
        'city_official',
        'county_official',
        'state_official',
        'press',
        'social',
        'other'
    ]));
