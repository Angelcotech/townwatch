-- Migration 020: GIS district polygons + seat ↔ district link.
--
-- jurisdiction_district stores one polygon per elected district
-- (commissioner districts, council wards, school zones, etc.) for a
-- jurisdiction. Geometry is WGS84 (SRID 4326) — matches ArcGIS REST's
-- default GeoJSON output and what every browser-side mapping library
-- expects, so no reprojection needed on either ingest or render.
--
-- MultiPolygon is the chosen geometry type because some districts have
-- discontiguous shapes (rare but real — Voting District 3 of County X
-- may include a separate island parcel). Polygon coerces cleanly into
-- MultiPolygon, so this also accepts single-polygon districts.
--
-- seat.district_id links each district-based seat to its polygon so
-- "what district is this address in?" → seat → official lookups are a
-- single join chain rather than a fuzzy name match.

CREATE TABLE jurisdiction_district (
    id              BIGSERIAL PRIMARY KEY,
    jurisdiction_id INTEGER NOT NULL REFERENCES jurisdiction(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    district_number INTEGER,
    geometry        GEOMETRY(MultiPolygon, 4326) NOT NULL,
    source_url      TEXT NOT NULL,
    data_source_id  INTEGER NOT NULL REFERENCES data_source(id),
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    UNIQUE (jurisdiction_id, name)
);

-- GIST index is required for ST_Contains / ST_Within to be fast.
-- Without it, point-in-polygon queries scan every row.
CREATE INDEX jurisdiction_district_geom_gix
    ON jurisdiction_district USING GIST (geometry);

CREATE INDEX jurisdiction_district_jurisdiction_idx
    ON jurisdiction_district (jurisdiction_id);

-- Link seats to their districts. Nullable because at-large seats have
-- no district and appointed seats aren't district-based at all.
ALTER TABLE seat
    ADD COLUMN district_id BIGINT REFERENCES jurisdiction_district(id) ON DELETE SET NULL;

CREATE INDEX seat_district_id_idx ON seat (district_id);
