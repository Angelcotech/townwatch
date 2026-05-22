-- Migration 015: records_request table
-- Tracks every records request prepared, sent, and resolved against a
-- compliance_finding. A finding can have multiple records_requests over
-- time (first request stalls → follow-up → escalation). The most recent
-- non-closed row is the active one.
--
-- Lifecycle:
--   draft (PDF generated, awaiting review by operator)
--   ready_for_review (auto-prepared, operator hasn't touched yet — same as draft for now)
--   sent (operator marked as actually sent; sent_to populated)
--   responded (clerk replied; classification pending or done)
--   closed (records arrived + ingested OR clerk confirmed no records exist)
--   failed (something broke; see pipeline_failure for details)

CREATE TABLE records_request (
    id                  SERIAL PRIMARY KEY,
    finding_id          INTEGER     NOT NULL REFERENCES compliance_finding (id),
    status              TEXT        NOT NULL DEFAULT 'draft'
                          CHECK (status IN (
                              'draft','ready_for_review','sent','responded','closed','failed'
                          )),
    -- Generated artifact
    pdf_path            TEXT,                   -- relative path within townwatch-web/public/
    pdf_generated_at    TIMESTAMPTZ,
    -- Review / send
    reviewed_at         TIMESTAMPTZ,
    reviewed_by         TEXT,                   -- operator identifier
    sent_at             TIMESTAMPTZ,
    sent_to             TEXT,                   -- email or mailing address actually used
    sent_method         TEXT,                   -- 'email','mail','hand_delivery'
    -- Response
    response_received_at TIMESTAMPTZ,
    response_artifacts   JSONB,                 -- list of {filename, content_type, storage_url}
    response_classification TEXT,               -- 'documents','extension','denial','non_responsive'
    response_notes       TEXT,
    -- Lifecycle
    closed_at           TIMESTAMPTZ,
    closed_reason       TEXT,
    -- Catch-all
    meta                JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_records_request_finding ON records_request (finding_id);
CREATE INDEX idx_records_request_status  ON records_request (status, created_at DESC);
