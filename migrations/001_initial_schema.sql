-- TownWatch — Initial Schema
-- Migration 001: Core data model
-- Created: 2026-05-13

-- ============================================================
-- EXTENSIONS
-- PostGIS deferred — added in a future migration when spatial
-- queries become required (Census TIGER boundaries, parcel maps).
-- For Phase 1, boundaries are stored as JSONB (GeoJSON shape).
-- ============================================================
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pg_trgm;


-- ============================================================
-- DATA SOURCE
-- Provenance for every record. Every other table carries a
-- data_source_id FK so the chain of custody is one join away.
-- ============================================================
CREATE TABLE data_source (
    id              SERIAL PRIMARY KEY,
    source_name     TEXT        NOT NULL,  -- e.g. 'FollowTheMoney', 'BallotReady', 'CensusTIGER'
    source_type     TEXT        NOT NULL,  -- 'api', 'bulk_download', 'scrape', 'manual'
    source_url      TEXT,
    record_url      TEXT,                  -- direct URL to this specific record/document
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    ingest_run_id   UUID,                  -- groups all records pulled in one ETL run
    raw_payload     JSONB,                 -- optional: store raw source record for reprocessing
    notes           TEXT
);

CREATE INDEX idx_data_source_source_name   ON data_source (source_name);
CREATE INDEX idx_data_source_ingested_at   ON data_source (ingested_at);
CREATE INDEX idx_data_source_ingest_run_id ON data_source (ingest_run_id);


-- ============================================================
-- JURISDICTION
-- A town, city, county, or special district (Census TIGER as
-- source of truth for boundaries and FIPS codes).
-- ============================================================
CREATE TABLE jurisdiction (
    id                  SERIAL PRIMARY KEY,
    fips_code           TEXT        NOT NULL UNIQUE,  -- 7-digit: 2-char state + 5-char place
    name                TEXT        NOT NULL,          -- official Census name, e.g. "Springfield city"
    display_name        TEXT        NOT NULL,          -- human-friendly, e.g. "Springfield"
    jurisdiction_type   TEXT        NOT NULL,          -- 'city','town','county','township','special_district'
    state_fips          CHAR(2)     NOT NULL,
    state_abbr          CHAR(2)     NOT NULL,
    county_fips         CHAR(5),
    population          INTEGER,
    boundary            JSONB,         -- GeoJSON MultiPolygon; promote to geography column when PostGIS lands
    tiger_vintage_year  SMALLINT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    data_source_id      INTEGER     NOT NULL REFERENCES data_source (id)
);

CREATE INDEX idx_jurisdiction_state_fips   ON jurisdiction (state_fips);
CREATE INDEX idx_jurisdiction_county_fips  ON jurisdiction (county_fips);
CREATE INDEX idx_jurisdiction_name_trgm    ON jurisdiction USING GIN (display_name gin_trgm_ops);
-- Spatial index on boundary deferred to PostGIS migration


-- ============================================================
-- GOVERNING BODY
-- A deliberative body within a jurisdiction (city council,
-- planning commission, school board, etc.)
-- ============================================================
CREATE TABLE governing_body (
    id                  SERIAL PRIMARY KEY,
    jurisdiction_id     INTEGER     NOT NULL REFERENCES jurisdiction (id),
    name                TEXT        NOT NULL,
    body_type           TEXT        NOT NULL,  -- 'city_council','county_board','school_board',
                                               -- 'planning_commission','water_board','other'
    description         TEXT,
    established_date    DATE,
    dissolved_date      DATE,
    meeting_frequency   TEXT,                  -- 'weekly','biweekly','monthly','as_needed'
    meeting_location    TEXT,
    website_url         TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    data_source_id      INTEGER     NOT NULL REFERENCES data_source (id),
    UNIQUE (jurisdiction_id, name)
);

CREATE INDEX idx_governing_body_jurisdiction ON governing_body (jurisdiction_id);
CREATE INDEX idx_governing_body_type         ON governing_body (body_type);


-- ============================================================
-- SEAT
-- A persistent position on a governing body. The seat exists
-- independent of who holds it — "Ward 3 Council Member" is the
-- same seat whether held by Alice in 2018 or Bob in 2022.
-- ============================================================
CREATE TABLE seat (
    id                      SERIAL PRIMARY KEY,
    governing_body_id       INTEGER     NOT NULL REFERENCES governing_body (id),
    name                    TEXT        NOT NULL,
    seat_type               TEXT        NOT NULL,  -- 'ward','at_large','district','appointed','ex_officio'
    district_name           TEXT,
    district_boundary       JSONB,         -- GeoJSON MultiPolygon; promote when PostGIS lands
    is_leadership           BOOLEAN     NOT NULL DEFAULT false,
    election_cycle_years    INTEGER[],
    term_length_years       SMALLINT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    data_source_id          INTEGER     NOT NULL REFERENCES data_source (id),
    UNIQUE (governing_body_id, name)
);

CREATE INDEX idx_seat_governing_body     ON seat (governing_body_id);
CREATE INDEX idx_seat_type               ON seat (seat_type);
-- Spatial index on district_boundary deferred to PostGIS migration


-- ============================================================
-- OFFICIAL
-- Canonical identity record for a person who holds or has held
-- a seat. Name variants across sources live in official_alias.
-- ============================================================
CREATE TABLE official (
    id                  SERIAL PRIMARY KEY,
    canonical_name      TEXT        NOT NULL,
    first_name          TEXT,
    middle_name         TEXT,
    last_name           TEXT        NOT NULL,
    suffix              TEXT,
    date_of_birth       DATE,
    gender              TEXT,
    party_affiliation   TEXT,
    email               TEXT,
    phone               TEXT,
    official_website    TEXT,
    photo_url           TEXT,
    bio_text            TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    data_source_id      INTEGER     NOT NULL REFERENCES data_source (id)
);

CREATE INDEX idx_official_last_name           ON official (last_name);
CREATE INDEX idx_official_canonical_name_trgm ON official USING GIN (canonical_name gin_trgm_ops);


-- ============================================================
-- OFFICIAL ALIAS
-- Every name variant for an official encountered across sources.
-- ETL pipelines resolve aliases to canonical official_id before
-- inserting votes, contributions, and property records.
-- ============================================================
CREATE TABLE official_alias (
    id              SERIAL PRIMARY KEY,
    official_id     INTEGER     NOT NULL REFERENCES official (id),
    alias_name      TEXT        NOT NULL,
    source_system   TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    data_source_id  INTEGER     NOT NULL REFERENCES data_source (id),
    UNIQUE (official_id, alias_name)
);

CREATE INDEX idx_official_alias_name_trgm ON official_alias USING GIN (alias_name gin_trgm_ops);
CREATE INDEX idx_official_alias_name      ON official_alias (alias_name);
CREATE INDEX idx_official_alias_official  ON official_alias (official_id);


-- ============================================================
-- TERM
-- A specific official's tenure in a specific seat. One official
-- can have many terms across many seats (full career history).
-- ============================================================
CREATE TABLE term (
    id                  SERIAL PRIMARY KEY,
    official_id         INTEGER     NOT NULL REFERENCES official (id),
    seat_id             INTEGER     NOT NULL REFERENCES seat (id),
    start_date          DATE        NOT NULL,
    end_date            DATE,                   -- NULL = currently serving
    election_cycle_year SMALLINT,
    how_seated          TEXT        NOT NULL,   -- 'elected','appointed','interim','recall_replacement'
    party_at_time       TEXT,
    ballot_name         TEXT,
    vote_share          NUMERIC(5,2),
    is_current          BOOLEAN     NOT NULL DEFAULT false,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    data_source_id      INTEGER     NOT NULL REFERENCES data_source (id)
);

CREATE INDEX idx_term_official        ON term (official_id);
CREATE INDEX idx_term_seat            ON term (seat_id);
CREATE INDEX idx_term_current         ON term (is_current) WHERE is_current = true;
CREATE INDEX idx_term_date_range      ON term (start_date, end_date);
CREATE INDEX idx_term_election_cycle  ON term (election_cycle_year);


-- ============================================================
-- MEETING
-- A public meeting of a governing body with links to agenda,
-- minutes, and video. Motions are parsed from these documents.
-- ============================================================
CREATE TABLE meeting (
    id                  SERIAL PRIMARY KEY,
    governing_body_id   INTEGER     NOT NULL REFERENCES governing_body (id),
    meeting_date        DATE        NOT NULL,
    meeting_time        TIME,
    meeting_type        TEXT        NOT NULL,   -- 'regular','special','emergency','workshop','executive_session'
    location            TEXT,
    agenda_url          TEXT,
    minutes_url         TEXT,
    video_url           TEXT,
    status              TEXT        NOT NULL,   -- 'scheduled','agenda_published','completed','minutes_published','cancelled'
    quorum_present      BOOLEAN,
    attendance_notes    TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    data_source_id      INTEGER     NOT NULL REFERENCES data_source (id)
);

CREATE INDEX idx_meeting_governing_body      ON meeting (governing_body_id);
CREATE INDEX idx_meeting_date                ON meeting (meeting_date DESC);
CREATE INDEX idx_meeting_governing_body_date ON meeting (governing_body_id, meeting_date DESC);


-- ============================================================
-- MOTION
-- An agenda item that was voted on. Could be an ordinance,
-- resolution, zoning change, budget amendment, appointment, etc.
-- ============================================================
CREATE TABLE motion (
    id                  SERIAL PRIMARY KEY,
    meeting_id          INTEGER     NOT NULL REFERENCES meeting (id),
    motion_number       TEXT,
    title               TEXT        NOT NULL,
    description         TEXT,
    motion_type         TEXT        NOT NULL,   -- 'ordinance','resolution','zoning_change',
                                               -- 'budget_amendment','appointment','contract_approval',
                                               -- 'procedural','other'
    full_text_url       TEXT,
    agenda_item_order   SMALLINT,
    outcome             TEXT,                   -- 'passed','failed','tabled','withdrawn','no_action'
    vote_tally_yes      SMALLINT,
    vote_tally_no       SMALLINT,
    vote_tally_abstain  SMALLINT,
    vote_tally_absent   SMALLINT,
    passed_at           TIMESTAMPTZ,
    effective_date      DATE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    data_source_id      INTEGER     NOT NULL REFERENCES data_source (id)
);

CREATE INDEX idx_motion_meeting  ON motion (meeting_id);
CREATE INDEX idx_motion_type     ON motion (motion_type);
CREATE INDEX idx_motion_outcome  ON motion (outcome);
CREATE INDEX idx_motion_number   ON motion (motion_number);
CREATE INDEX idx_motion_fts      ON motion USING GIN (
    to_tsvector('english', coalesce(title, '') || ' ' || coalesce(description, ''))
);


-- ============================================================
-- VOTE
-- A single official's recorded vote on a single motion.
-- ============================================================
CREATE TABLE vote (
    id              SERIAL PRIMARY KEY,
    official_id     INTEGER     NOT NULL REFERENCES official (id),
    motion_id       INTEGER     NOT NULL REFERENCES motion (id),
    term_id         INTEGER     REFERENCES term (id),
    vote_value      TEXT        NOT NULL,   -- 'yes','no','abstain','absent','not_eligible','conflict_recusal'
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    data_source_id  INTEGER     NOT NULL REFERENCES data_source (id),
    UNIQUE (official_id, motion_id)
);

CREATE INDEX idx_vote_official ON vote (official_id);
CREATE INDEX idx_vote_motion   ON vote (motion_id);
CREATE INDEX idx_vote_term     ON vote (term_id);
CREATE INDEX idx_vote_value    ON vote (vote_value);


-- ============================================================
-- CAMPAIGN CONTRIBUTION
-- A donation to an official's campaign. Source: FollowTheMoney.
-- Donor identity is stored as cleaned free text from FTM's own
-- normalization — not resolved to a separate donor entity.
-- ============================================================
CREATE TABLE campaign_contribution (
    id                      SERIAL PRIMARY KEY,
    official_id             INTEGER       NOT NULL REFERENCES official (id),
    election_cycle_year     SMALLINT      NOT NULL,
    contributor_name        TEXT          NOT NULL,
    contributor_type        TEXT,          -- 'individual','corporation','pac','union','party','other'
    contributor_employer    TEXT,
    contributor_occupation  TEXT,
    contributor_city        TEXT,
    contributor_state       CHAR(2),
    contributor_zip         TEXT,
    amount                  NUMERIC(12,2) NOT NULL,
    contribution_date       DATE,
    recipient_committee     TEXT,
    transaction_type        TEXT,          -- 'monetary','in_kind','loan'
    ftm_transaction_id      TEXT UNIQUE,   -- FollowTheMoney transaction ID for deduplication
    created_at              TIMESTAMPTZ   NOT NULL DEFAULT now(),
    data_source_id          INTEGER       NOT NULL REFERENCES data_source (id)
);

CREATE INDEX idx_cc_official           ON campaign_contribution (official_id);
CREATE INDEX idx_cc_cycle              ON campaign_contribution (election_cycle_year);
CREATE INDEX idx_cc_official_cycle     ON campaign_contribution (official_id, election_cycle_year);
CREATE INDEX idx_cc_date               ON campaign_contribution (contribution_date DESC);
CREATE INDEX idx_cc_amount             ON campaign_contribution (amount DESC);
CREATE INDEX idx_cc_contributor        ON campaign_contribution (contributor_name);
CREATE INDEX idx_cc_contributor_trgm   ON campaign_contribution USING GIN (contributor_name gin_trgm_ops);
CREATE INDEX idx_cc_type               ON campaign_contribution (contributor_type);


-- ============================================================
-- PROPERTY RECORD
-- Annual assessed value snapshot for a property owned by an
-- official. One row per parcel per year — never update, always
-- insert. This makes the table a time series by construction.
-- ============================================================
CREATE TABLE property_record (
    id                      SERIAL PRIMARY KEY,
    official_id             INTEGER       NOT NULL REFERENCES official (id),
    assessment_year         SMALLINT      NOT NULL,
    parcel_id               TEXT          NOT NULL,
    situs_address           TEXT,
    situs_city              TEXT,
    situs_state             CHAR(2),
    situs_zip               TEXT,
    situs_county            TEXT,
    legal_description       TEXT,
    property_use_code       TEXT,
    property_type           TEXT,          -- 'residential','commercial','agricultural','vacant','other'
    land_area_sqft          NUMERIC(12,2),
    building_sqft           NUMERIC(12,2),
    year_built              SMALLINT,
    assessed_value_land     NUMERIC(14,2),
    assessed_value_building NUMERIC(14,2),
    assessed_value_total    NUMERIC(14,2),
    market_value            NUMERIC(14,2),
    exemptions              TEXT[],
    owner_name_raw          TEXT,          -- exact string from assessor (preserved for audit)
    ownership_type          TEXT,          -- 'sole','joint','trust','llc','other'
    deed_recorded_date      DATE,
    location                JSONB,         -- GeoJSON Point [lon, lat]; promote when PostGIS lands
    created_at              TIMESTAMPTZ   NOT NULL DEFAULT now(),
    data_source_id          INTEGER       NOT NULL REFERENCES data_source (id),
    UNIQUE (official_id, parcel_id, assessment_year)
);

CREATE INDEX idx_pr_official      ON property_record (official_id);
CREATE INDEX idx_pr_official_year ON property_record (official_id, assessment_year DESC);
CREATE INDEX idx_pr_parcel        ON property_record (parcel_id);
CREATE INDEX idx_pr_year          ON property_record (assessment_year);
-- Spatial index on location deferred to PostGIS migration
CREATE INDEX idx_pr_owner_name    ON property_record (owner_name_raw);
CREATE INDEX idx_pr_total_value   ON property_record (assessed_value_total DESC);
