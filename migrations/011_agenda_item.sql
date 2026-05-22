-- Migration 011: agenda_item table
-- Docket-level data extracted from agenda PDFs (separate from motion, which
-- captures the council's action on the item). An agenda lists what WILL BE
-- discussed — items, applicants, hearing topics, recommended actions — but
-- not how the body voted. Minutes capture the action; agendas capture the
-- request. Both run automatically for every meeting that has the
-- corresponding PDF.

CREATE TABLE agenda_item (
    id                   SERIAL PRIMARY KEY,
    meeting_id           INTEGER     NOT NULL REFERENCES meeting (id),
    item_number          TEXT,                                -- e.g. "7A", "IV.2"
    title                TEXT        NOT NULL,
    description          TEXT,                                -- one-paragraph staff write-up if present
    item_type            TEXT        NOT NULL,                -- 'rezoning','variance','special_exception',
                                                              -- 'conditional_use','subdivision','annexation',
                                                              -- 'ordinance_amendment','public_hearing',
                                                              -- 'consent','presentation','old_business',
                                                              -- 'new_business','staff_report','procedural','other'
    applicant_name       TEXT,                                -- Who applied/petitioned — the request originator
    recommended_action   TEXT,                                -- What staff is asking the body to do
                                                              -- ("approve", "deny", "table", "no recommendation")
    hearing_status       TEXT,                                -- 'first_reading','second_reading','public_hearing',
                                                              -- 'continued','rescheduled', NULL when not applicable
    locations            JSONB,                               -- Property addresses / parcel IDs (same shape as motion.locations)
    documents_referenced JSONB,                               -- Attachments, exhibits, staff reports
    source_page          SMALLINT,                            -- 1-indexed PDF page where the item begins
    meta                 JSONB,                               -- Catch-all
    data_status          TEXT        NOT NULL DEFAULT 'clean'
        CHECK (data_status IN ('clean', 'repairing', 'disputed')),
    data_status_reason   TEXT,
    data_status_at       TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    data_source_id       INTEGER     NOT NULL REFERENCES data_source (id)
);

CREATE INDEX idx_agenda_item_meeting        ON agenda_item (meeting_id);
CREATE INDEX idx_agenda_item_type           ON agenda_item (item_type);
CREATE INDEX idx_agenda_item_data_status    ON agenda_item (data_status);
CREATE INDEX idx_agenda_item_applicant_trgm ON agenda_item USING GIN (applicant_name gin_trgm_ops);
CREATE INDEX idx_agenda_item_locations_gin  ON agenda_item USING GIN (locations);
CREATE INDEX idx_agenda_item_fts            ON agenda_item USING GIN (
    to_tsvector('english', coalesce(title, '') || ' ' || coalesce(description, ''))
);

-- Idempotency: re-running the extractor for a meeting won't duplicate items.
-- Using meeting_id + lower(title) as the conflict key because item_number is
-- often missing on PC/BZA agendas (numbered only by section, not item).
CREATE UNIQUE INDEX uniq_agenda_item_meeting_title
    ON agenda_item (meeting_id, lower(title));


-- ============================================================
-- motion ← agenda_item link (deferred — populated by a future job that
-- cross-references extracted minutes against extracted agendas for the
-- same meeting). Nullable so the link is opt-in and doesn't gate either
-- extractor.
-- ============================================================
ALTER TABLE motion
    ADD COLUMN agenda_item_id INTEGER REFERENCES agenda_item (id);

CREATE INDEX idx_motion_agenda_item ON motion (agenda_item_id);
