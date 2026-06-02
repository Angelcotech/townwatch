-- Migration 032: data_correction — the public error-report inbox.
--
-- TownWatch's credibility rests on being a faithful mirror of the public
-- record. A faithful mirror needs a way for the public to say "this reflection
-- is wrong" — and a logged, reviewable trail of what was reported and how it
-- was resolved. That correction loop is also the legal keystone: a system that
-- cites its sources AND fixes errors promptly when flagged is the opposite of
-- reckless, which is what defamation liability for public-figure reporting
-- turns on.
--
-- This is an append-only intake table. A citizen reports that a specific datum
-- (a motion, a vote, a finding, a meeting field) looks wrong; one row is
-- written. Triage updates status + resolution. Writes happen ONLY in the ETL
-- domain (a small intake service) — the web layer stays read-only.
--
-- Deliberately, an incoming report does NOT mutate the referenced datum. A
-- report flips nothing to 'disputed' on its own, because data_status='disputed'
-- removes a row from the live record, and anonymous input must never be able to
-- silently suppress an inconvenient vote. Acceptance during triage is what
-- changes data; intake only records the claim.

CREATE TABLE IF NOT EXISTS data_correction (
    id                BIGSERIAL PRIMARY KEY,
    entity_type       TEXT NOT NULL,              -- 'motion','vote','finding','meeting','official', etc.
    entity_id         BIGINT NOT NULL,            -- the disputed row's id
    field             TEXT,                       -- optional specific field in dispute
    reported_issue    TEXT NOT NULL,              -- citizen's description of what's wrong
    suggested_value   TEXT,                       -- optional: what they say it should say
    source_note       TEXT,                       -- optional: citizen's own citation / where to verify
    reporter_contact  TEXT,                       -- optional email for follow-up (may be NULL/anonymous)
    reporter_ip       INET,                       -- for abuse triage / rate-limiting (nullable)
    status            TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open','reviewing','accepted','rejected','duplicate')),
    resolution_note   TEXT,                       -- triager's note on how it was handled
    resolved_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_data_correction_status
    ON data_correction (status);
CREATE INDEX IF NOT EXISTS idx_data_correction_entity
    ON data_correction (entity_type, entity_id);

-- Lightweight anti-spam guard: dedupe identical open reports on the same datum
-- so a refresh/double-click doesn't create stacks of the same row. Different
-- wording still gets its own row; an exact repeat collapses.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_data_correction_open
    ON data_correction (entity_type, entity_id, md5(reported_issue))
    WHERE status = 'open';
