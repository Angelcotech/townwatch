-- Migration 016: campaign_filing table
-- A single submitted Campaign Contribution Disclosure Report (CCDR) or
-- equivalent — pre-election, post-election, semi-annual, or two-business-day
-- large-contribution report. Each filing covers one official, one election
-- cycle, one reporting period. Multiple contributions roll up under it.
--
-- This is the document-level record. When a records request returns a
-- batch of CCDRs from the City Clerk, each becomes one campaign_filing
-- row; the extractor then writes individual campaign_contribution rows
-- pointing back via filing_id.

CREATE TABLE campaign_filing (
    id                  SERIAL PRIMARY KEY,
    official_id         INTEGER     NOT NULL REFERENCES official (id),
    election_cycle_year SMALLINT    NOT NULL,
    filing_type         TEXT        NOT NULL  -- 'pre_election','post_election',
                                              -- 'semi_annual','two_business_day_large',
                                              -- 'amendment','final','other'
                          CHECK (filing_type IN (
                              'pre_election','post_election','semi_annual',
                              'two_business_day_large','amendment','final','other'
                          )),
    filing_period_start DATE,
    filing_period_end   DATE,
    filing_date         DATE,                 -- when the candidate actually filed
    source_document_url TEXT,                 -- where we got the PDF (records request response, portal link)
    source_format       TEXT,                 -- 'pdf','docx','doc','scan','data_export'
    -- Totals declared on the filing's summary page — useful for QA
    -- (does sum of campaign_contribution.amount match this declared total?)
    declared_total_contributions NUMERIC(14,2),
    declared_total_expenditures  NUMERIC(14,2),
    declared_cash_on_hand        NUMERIC(14,2),
    -- Raw extraction payload from the AI parser, kept for audit
    raw_extraction      JSONB,
    extraction_method   TEXT,                 -- 'text_layer','vision','docx_text','manual_entry'
    extraction_confidence TEXT
                          CHECK (extraction_confidence IS NULL OR extraction_confidence IN ('high','medium','low')),
    data_status         TEXT        NOT NULL DEFAULT 'clean'
                          CHECK (data_status IN ('clean','repairing','disputed')),
    data_status_reason  TEXT,
    data_status_at      TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    data_source_id      INTEGER     NOT NULL REFERENCES data_source (id),
    -- One filing of a given type per (official, cycle, period_end). Amendments
    -- get their own row with filing_type='amendment'.
    UNIQUE (official_id, election_cycle_year, filing_type, filing_period_end)
);

CREATE INDEX idx_campaign_filing_official ON campaign_filing (official_id);
CREATE INDEX idx_campaign_filing_cycle    ON campaign_filing (election_cycle_year);
CREATE INDEX idx_campaign_filing_status   ON campaign_filing (data_status);

-- Link contributions back to their source filing. Nullable because
-- contributions ingested from data exports (e.g. FollowTheMoney bulk
-- data) won't have a single document source.
ALTER TABLE campaign_contribution
    ADD COLUMN campaign_filing_id INTEGER REFERENCES campaign_filing (id);

CREATE INDEX idx_cc_filing ON campaign_contribution (campaign_filing_id);
