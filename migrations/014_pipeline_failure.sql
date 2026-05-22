-- Migration 014: pipeline_failure table
-- Loud failure surface. Every audit-pipeline job writes to this table when
-- something goes wrong — extraction error, refresh error, network failure,
-- AI classifier error, etc. The admin queue page shows unresolved failures
-- prominently so problems surface instead of disappearing silently.
--
-- Resolution is manual for now: an operator reviews, fixes the root cause
-- (or marks as known), and sets resolved_at + resolution_notes.

CREATE TABLE pipeline_failure (
    id              SERIAL PRIMARY KEY,
    job_name        TEXT        NOT NULL,   -- e.g. 'extract_agendas', 'refresh_findings'
    step            TEXT,                   -- finer-grained step within the job
    -- Optional contextual FKs — set whichever applies. Nullable so any job can use this.
    governing_body_id INTEGER REFERENCES governing_body (id),
    meeting_id        INTEGER REFERENCES meeting (id),
    finding_id        INTEGER REFERENCES compliance_finding (id),
    -- The error itself
    exception_class TEXT,                   -- e.g. 'ValueError'
    message         TEXT        NOT NULL,
    context         JSONB,                  -- caller-provided context (url, args, etc.)
    traceback       TEXT,
    -- Lifecycle
    created_at      TIMESTAMPTZ NOT NULL    DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    resolution_notes TEXT
);

CREATE INDEX idx_pipeline_failure_unresolved ON pipeline_failure (created_at DESC) WHERE resolved_at IS NULL;
CREATE INDEX idx_pipeline_failure_job        ON pipeline_failure (job_name, created_at DESC);
CREATE INDEX idx_pipeline_failure_body       ON pipeline_failure (governing_body_id) WHERE governing_body_id IS NOT NULL;
CREATE INDEX idx_pipeline_failure_finding    ON pipeline_failure (finding_id) WHERE finding_id IS NOT NULL;
