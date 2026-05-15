-- Migration 006: official_photo table
-- Multi-candidate, verifiable photos per official. Public surfaces only
-- display photos with data_status='verified'. Unverified candidates sit
-- in an operator queue for approve/reject.

CREATE TABLE official_photo (
    id                 SERIAL PRIMARY KEY,
    official_id        INT NOT NULL REFERENCES official(id) ON DELETE CASCADE,

    -- The photo itself
    photo_url          TEXT NOT NULL,          -- where the photo lives
    photo_bytes        BYTEA,                  -- cached local copy (so we don't depend on upstream availability)
    photo_mime         TEXT,                   -- image/jpeg, image/png

    -- Where we found it
    source_url         TEXT NOT NULL,          -- page that displayed the photo
    source_tier        TEXT NOT NULL CHECK (source_tier IN
                          ('city_official', 'state_official', 'press', 'social', 'other')),
    source_caption     TEXT,                   -- alt text or near-by caption
    source_platform    TEXT,                   -- civicengage, granicus, wordpress, vision_fallback, ...

    -- Verification scoring (0..3+)
    -- +1 source authority (URL is on official jurisdiction domain)
    -- +1 caption contains canonical name
    -- +1 vision check confirms identity
    verification_score INT NOT NULL DEFAULT 0,
    vision_confidence  TEXT CHECK (vision_confidence IN ('high', 'medium', 'low')),

    -- Status
    data_status        TEXT NOT NULL DEFAULT 'unverified'
                          CHECK (data_status IN ('verified', 'unverified', 'disputed')),
    verified_at        TIMESTAMPTZ,
    verified_by        TEXT,                   -- 'auto:<reason>' or operator id

    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (official_id, photo_url)
);

CREATE INDEX idx_official_photo_official ON official_photo(official_id, data_status);
CREATE INDEX idx_official_photo_unverified ON official_photo(data_status) WHERE data_status = 'unverified';
