-- Migration 004: Comprehensive motion + meeting fields
-- Adds the upstream-attribution and structured fields we need to surface
-- the full decision-making pipeline, not just the visible vote.

-- ============================================================
-- MOTION — upstream attribution and structured fields
-- ============================================================
ALTER TABLE motion
    ADD COLUMN petitioner_name      TEXT,          -- Who requested/applied for this (developer, business, resident)
    ADD COLUMN staff_recommender    TEXT,          -- Which staff member recommended approval (Director X)
    ADD COLUMN presenter            TEXT,          -- Who presented the item to council
    ADD COLUMN movant               TEXT,          -- Who made the motion
    ADD COLUMN seconder             TEXT,          -- Who seconded
    ADD COLUMN discussion_summary   TEXT,          -- 1-3 sentence summary of council deliberation
    ADD COLUMN dollar_amount        NUMERIC(14,2), -- $ value when stated (contract, budget, etc.)
    ADD COLUMN documents_referenced JSONB,         -- List of reports/attachments cited
    ADD COLUMN locations            JSONB,         -- Array of {label, parcel_id, address}
    ADD COLUMN meta                 JSONB;         -- Catch-all for less-queried fields

CREATE INDEX idx_motion_petitioner_trgm        ON motion USING GIN (petitioner_name gin_trgm_ops);
CREATE INDEX idx_motion_staff_recommender_trgm ON motion USING GIN (staff_recommender gin_trgm_ops);
CREATE INDEX idx_motion_dollar                 ON motion (dollar_amount DESC NULLS LAST);
CREATE INDEX idx_motion_locations_gin          ON motion USING GIN (locations);


-- ============================================================
-- MEETING — staff presence and timing fields
-- (video_url, status, attendance_notes, quorum_present already exist)
-- ============================================================
ALTER TABLE meeting
    ADD COLUMN called_to_order_at TIME,
    ADD COLUMN adjourned_at       TIME,
    ADD COLUMN staff_present      JSONB,  -- Array of {name, title}
    ADD COLUMN others_present     JSONB,  -- Array of strings (county reps, citizens, etc.)
    ADD COLUMN meta               JSONB;  -- Catch-all for less-queried fields
