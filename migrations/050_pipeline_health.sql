-- Migration 050: pipeline_run + pipeline_issue
-- Operational health for the automation itself — distinct from compliance_finding
-- (which audits the published RECORDS). These two tables answer "is the pipeline
-- doing its job for each jurisdiction, and what needs a human/agent to fix?"
--
--  * pipeline_run  — a heartbeat. One row per per-jurisdiction daily_refresh run:
--    when it ran, the outcome, which steps ran/were skipped, and what it surfaced
--    (new meetings/agendas/minutes/roster). Silence in this table = the pipeline
--    stopped running, which is itself the signal.
--
--  * pipeline_issue — a deduplicated, resolvable problem. Mirrors compliance_finding's
--    discipline (one open row per problem via a UNIQUE key; re-observe refreshes
--    last_observed_at; recurrence after resolve reopens). Written/observed by
--    jobs/refresh_pipeline_health.py and daily_refresh; triaged via the
--    pipeline_issues CLI (Claude Code) and the admin "Pipeline health" tab (human).
--    The raw per-occurrence log (pipeline_failure) is left intact; this is the
--    actionable rollup on top of it.

CREATE TABLE IF NOT EXISTS pipeline_run (
    id              SERIAL      PRIMARY KEY,
    jurisdiction_id INTEGER     NOT NULL REFERENCES jurisdiction (id) ON DELETE CASCADE,
    trigger         TEXT        NOT NULL DEFAULT 'cron',   -- 'cron' | 'deposit' | 'manual'
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    outcome         TEXT        NOT NULL DEFAULT 'ok'
                      CHECK (outcome IN ('ok', 'partial', 'failed', 'paused')),
    steps           JSONB       NOT NULL DEFAULT '[]'::jsonb,  -- [{module, ok, skipped}]
    surfaced        JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- {meetings, agendas, motions, roster}
    error_count     INTEGER     NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_pipeline_run_juris_time
    ON pipeline_run (jurisdiction_id, started_at DESC);


CREATE TABLE IF NOT EXISTS pipeline_issue (
    id               SERIAL      PRIMARY KEY,
    jurisdiction_id  INTEGER     NOT NULL REFERENCES jurisdiction (id) ON DELETE CASCADE,
    issue_type       TEXT        NOT NULL,   -- 'step_failed' | 'pipeline_stale' | 'health_check'
    severity         TEXT        NOT NULL DEFAULT 'medium'
                       CHECK (severity IN ('low', 'medium', 'high')),
    title            TEXT        NOT NULL,   -- one-line summary
    detail           TEXT,                   -- the clear message + how to diagnose
    status           TEXT        NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open', 'resolved', 'wont_fix')),
    first_observed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_observed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at      TIMESTAMPTZ,
    resolved_by      TEXT,                   -- 'claude-code' | 'admin' | ...
    diagnosis        TEXT,                   -- root cause (filled at resolve time)
    fix_notes        TEXT,                   -- what was changed
    context          JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- job_name, step, meeting_id, url, failure_ids, ...
    dedupe_key       TEXT        NOT NULL,   -- stable id for the problem (e.g. 'step_failed:extract_minutes')
    -- One row per (jurisdiction, problem). Re-observe updates in place; a recurrence
    -- after resolution reopens the same row rather than spawning a duplicate.
    UNIQUE (jurisdiction_id, dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_issue_open
    ON pipeline_issue (last_observed_at DESC) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_pipeline_issue_juris
    ON pipeline_issue (jurisdiction_id);
