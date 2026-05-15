-- Migration 005: data_status quarantine column
-- Enables the self-QA protocol: motions with unresolved QA findings are
-- automatically marked 'disputed' and hidden from public surfaces. Only
-- motions with data_status='clean' are considered published.

ALTER TABLE motion
    ADD COLUMN data_status        TEXT NOT NULL DEFAULT 'clean'
        CHECK (data_status IN ('clean', 'repairing', 'disputed')),
    ADD COLUMN data_status_reason TEXT,                          -- last QA pattern_id that flagged it
    ADD COLUMN data_status_at     TIMESTAMPTZ;                   -- when it was last quarantined / cleared

CREATE INDEX idx_motion_data_status ON motion (data_status);
