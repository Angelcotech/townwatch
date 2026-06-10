-- Migration 054: org-level pipeline issues (nullable jurisdiction_id)
--
-- Some operational problems aren't per-jurisdiction — a missing API key, a dead
-- dependency, a broken cron — they affect the whole environment. pipeline_issue
-- required a jurisdiction_id, so it couldn't represent them. Make it nullable
-- (NULL = org-level) and keep dedup working across the NULL via a NULL-safe unique
-- index (the table-level UNIQUE treated every NULL as distinct, which would let
-- duplicate org-level issues pile up).

ALTER TABLE pipeline_issue ALTER COLUMN jurisdiction_id DROP NOT NULL;

ALTER TABLE pipeline_issue DROP CONSTRAINT IF EXISTS pipeline_issue_jurisdiction_id_dedupe_key_key;

-- One issue per (jurisdiction, dedupe_key); org-level rows (NULL) collapse to the
-- sentinel 0, which no real jurisdiction id occupies, so they dedupe too.
CREATE UNIQUE INDEX IF NOT EXISTS uq_pipeline_issue_dedupe
    ON pipeline_issue (COALESCE(jurisdiction_id, 0), dedupe_key);
