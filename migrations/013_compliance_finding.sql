-- Migration 013: compliance_finding table
-- Persists the audit findings TownWatch generates against published
-- records of each governing body. Each row asserts a gap between what
-- state/local law requires and what the body has made public, cites the
-- statute, and carries a lifecycle status so progress closing the gap
-- is queryable over time.
--
-- Refresh job (jobs/refresh_findings.py) recomputes the underlying gap
-- from current meeting data and upserts. Status is preserved across
-- refreshes; a previously-recorded finding that no longer applies
-- (because records arrived) transitions to closed_with_records.

CREATE TABLE compliance_finding (
    id                  SERIAL PRIMARY KEY,
    governing_body_id   INTEGER     NOT NULL REFERENCES governing_body (id),
    category            TEXT        NOT NULL,   -- 'minutes_missing','agenda_missing',
                                                -- 'campaign_finance_missing','attendance_missing','other'
    severity            TEXT        NOT NULL    -- 'high','medium','low'
                          CHECK (severity IN ('high','medium','low')),
    statute_label       TEXT        NOT NULL,   -- e.g. 'OCGA § 50-14-1(e)(2)'
    statute_url         TEXT        NOT NULL,
    statute_text        TEXT        NOT NULL,   -- short paraphrase of the requirement
    count               INTEGER     NOT NULL,   -- number of affected items at observation time
    since_date          DATE,                   -- earliest affected meeting date if applicable
    status              TEXT        NOT NULL    DEFAULT 'open'
                          CHECK (status IN (
                              'open',
                              'records_requested',
                              'closed_with_records',
                              'closed_as_unrecoverable',
                              'closed_acknowledged'
                          )),
    opened_at           TIMESTAMPTZ NOT NULL    DEFAULT now(),
    last_observed_at    TIMESTAMPTZ NOT NULL    DEFAULT now(),
    closed_at           TIMESTAMPTZ,
    closed_reason       TEXT,
    meta                JSONB,                  -- catch-all (records_request_sent_at, response notes, etc.)
    -- One open finding per (body, category). A new finding can be re-opened
    -- after a previous close via UPDATE rather than a new row.
    UNIQUE (governing_body_id, category)
);

CREATE INDEX idx_compliance_finding_body     ON compliance_finding (governing_body_id);
CREATE INDEX idx_compliance_finding_status   ON compliance_finding (status);
CREATE INDEX idx_compliance_finding_category ON compliance_finding (category);
