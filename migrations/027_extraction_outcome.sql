-- Migration 027: extraction_outcome — the success-rate ledger.
--
-- Extraction runs (extract_minutes / extract_agendas) print a per-run
-- clean / recovered / failed breakdown and a success rate, then it
-- evaporates to stdout. This table persists one row per meeting extraction
-- so the success rate becomes a queryable artifact — sliceable by
-- jurisdiction, by job, by time, by recovery outcome — instead of a
-- transient log line.
--
-- Why it matters: this is the rollout-confidence number ("what % of
-- extractions produce a usable record"), the maintenance early-warning
-- (does cost/quality-per-town stay flat as N grows), and the audit trail
-- for a project whose whole pitch is measurable transparency.
--
-- outcome:
--   'clean'     — produced a record, every window resolved on first try
--   'recovered' — produced a record, but the escalating ladder had to
--                 recover one+ windows (still a success, just worked for it)
--   'failed'    — no record produced (extraction raised); the irreducible case
-- "Produced a record" (the headline success rate) = clean + recovered.

CREATE TABLE IF NOT EXISTS extraction_outcome (
    id                SERIAL PRIMARY KEY,
    run_id            UUID        NOT NULL,                 -- groups one job invocation
    job_name          TEXT        NOT NULL,                 -- 'extract_minutes' | 'extract_agendas'
    meeting_id        INTEGER     REFERENCES meeting (id),
    jurisdiction_id   INTEGER     REFERENCES jurisdiction (id),
    outcome           TEXT        NOT NULL,                 -- 'clean' | 'recovered' | 'failed'
    units_total       INTEGER     NOT NULL DEFAULT 0,       -- page-windows resolved
    units_clean       INTEGER     NOT NULL DEFAULT 0,
    units_recovered   INTEGER     NOT NULL DEFAULT 0,
    units_anomaly     INTEGER     NOT NULL DEFAULT 0,
    anomaly_kinds     JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- {"truncation_irreducible": 1, ...}
    method            TEXT,                                 -- 'text_layer' | 'vision' | 'stub_skipped' | ...
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_extraction_outcome_run          ON extraction_outcome (run_id);
CREATE INDEX IF NOT EXISTS idx_extraction_outcome_juris_time   ON extraction_outcome (jurisdiction_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_extraction_outcome_job_time     ON extraction_outcome (job_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_extraction_outcome_outcome      ON extraction_outcome (outcome);
