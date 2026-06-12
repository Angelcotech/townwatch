-- campaign_filing: one row per source DOCUMENT, not per (official, period).
--
-- The original UNIQUE (official_id, election_cycle_year, filing_type,
-- filing_period_end) assumed one filing per official per period. Real GA
-- local filings break that: an official can hold/run for TWO offices with
-- separate campaign committees and file a CCDR for each in the same period
-- (found 2026-06-12: Alison Couch filed June-30-2025 CCDRs as both District 4
-- Commissioner and Commission Chairman — the second, 61-contribution filing
-- was silently discarded as a "duplicate" of the first).
--
-- The honest dedupe key is the source document itself: the ingestor already
-- skips URLs it has ingested, and a partial unique index makes that durable
-- at the schema level.

ALTER TABLE campaign_filing
    DROP CONSTRAINT IF EXISTS campaign_filing_official_id_election_cycle_year_filing_type_key;

CREATE UNIQUE INDEX IF NOT EXISTS campaign_filing_source_document_uniq
    ON campaign_filing (source_document_url)
    WHERE source_document_url IS NOT NULL;
