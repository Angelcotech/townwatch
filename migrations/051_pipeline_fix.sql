-- Migration 051: pipeline_fix — the append-only fix log (resolution knowledge base).
--
-- pipeline_issue.fix_notes/diagnosis hold only the LAST resolution and are
-- overwritten on the next resolve, so they can't accumulate institutional memory.
-- This table keeps EVERY resolution, keyed by the problem's dedupe_key (which is
-- jurisdiction-independent — e.g. 'failure:extract_minutes:extract_all'), so when
-- the same class of problem recurs (same town later, or a different town) the
-- prior diagnoses + fixes surface at triage time instead of being re-derived.
--
-- Written only on a human/agent resolve (pipeline_health.resolve_issue) — NOT on
-- the observer's auto-clear, which isn't a troubleshooting fix. Read by the
-- pipeline_issues CLI: `show` surfaces prior fixes for the issue's class, and
-- `fixes` lists the whole knowledge base.

CREATE TABLE IF NOT EXISTS pipeline_fix (
    id              SERIAL      PRIMARY KEY,
    issue_id        INTEGER     REFERENCES pipeline_issue (id) ON DELETE SET NULL,
    jurisdiction_id INTEGER     REFERENCES jurisdiction (id) ON DELETE CASCADE,
    dedupe_key      TEXT        NOT NULL,   -- the problem CLASS (jurisdiction-independent)
    resolution      TEXT        NOT NULL,   -- 'resolved' | 'wont_fix'
    diagnosis       TEXT,                   -- why it broke (root cause)
    fix_notes       TEXT,                   -- what was changed
    resolved_by     TEXT,                   -- 'claude-code' | 'admin' | ...
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pipeline_fix_dedupe ON pipeline_fix (dedupe_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_fix_time   ON pipeline_fix (created_at DESC);
