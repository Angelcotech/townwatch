-- TownWatch — Pattern detection findings
-- Migration 003: Each finding is a sentence-level pattern detection emitted
-- by a Pattern detector. Re-runs replace all findings for that pattern_id
-- (the runner clears + re-inserts atomically).

CREATE TABLE finding (
    id                  SERIAL      PRIMARY KEY,
    pattern_id          TEXT        NOT NULL,    -- e.g. 'unanimity_rate'
    severity            SMALLINT    NOT NULL CHECK (severity BETWEEN 1 AND 5),
    title               TEXT        NOT NULL,    -- shareable one-sentence finding
    explanation         TEXT,                    -- plain-English why this is flagged
    jurisdiction_id     INTEGER     REFERENCES jurisdiction (id),
    governing_body_id   INTEGER     REFERENCES governing_body (id),
    subject_official_id INTEGER     REFERENCES official (id),
    subject_motion_id   INTEGER     REFERENCES motion (id),
    evidence            JSONB,                   -- list of receipts (motion ids, vote ids, dates)
    metrics             JSONB,                   -- numeric stats backing the finding
    detected_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    data_source_id      INTEGER     REFERENCES data_source (id)
);

CREATE INDEX idx_finding_pattern    ON finding (pattern_id);
CREATE INDEX idx_finding_official   ON finding (subject_official_id);
CREATE INDEX idx_finding_body       ON finding (governing_body_id);
CREATE INDEX idx_finding_severity   ON finding (severity DESC);
CREATE INDEX idx_finding_detected   ON finding (detected_at DESC);
