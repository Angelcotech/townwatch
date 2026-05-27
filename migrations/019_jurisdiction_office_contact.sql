-- Migration 019: add primary office address + phone to jurisdiction.
-- These are the citizen-facing contact points (city hall for cities,
-- commissioners office for counties, etc.). Surfaced in the frontend
-- as the always-public point of contact, separate from individual
-- officials' direct contact info which often isn't published.
--
-- Neutral column names ("office_" rather than "city_hall_") so they
-- read correctly across jurisdiction types (city/county/school district).
-- The per-jurisdiction config file still uses the legacy field names
-- (city_hall_address / city_hall_phone) for now; the sync ETL maps.

ALTER TABLE jurisdiction
    ADD COLUMN office_address TEXT,
    ADD COLUMN office_phone   TEXT;
