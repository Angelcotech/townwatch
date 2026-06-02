-- Migration 031: datum-level provenance + extraction honesty on motion & vote
--
-- Until now a motion/vote was traceable only to its MEETING (one minutes PDF
-- for the whole meeting) — never to the page it was read from, and with no
-- record of HOW it was read (clean text layer vs. shaky vision/OCR) or how
-- confident the extractor was. That opacity is both a credibility gap and the
-- exact liability surface for a public-record tool: an authoritative-looking
-- datum the reader can't verify and we can't qualify.
--
-- This adds three columns to each of motion and vote so every displayed datum
-- can (a) deep-link to the source PDF page it came from, and (b) carry an
-- honest signal of how it was extracted and how sure we are. The source page
-- is ALREADY captured by the extractor (minutes.AgendaItem.source_page) and
-- simply discarded at ingest — this stops discarding it. agenda_item already
-- has source_page; this brings motion/vote up to the same standard.
--
-- vote.attribution records whether an individual vote was NAMED in the minutes
-- ('recorded') or would be derived from a bare tally ('inferred'). Today the
-- ingest never fabricates a vote from a tally, so every existing row is a
-- recorded vote — hence the 'recorded' default is the honest backfill value,
-- and it lets the UI truthfully say "individual votes shown were named in the
-- official minutes." The column future-proofs the day we choose to surface
-- tally-derived votes, which must then be visibly marked 'inferred'.
--
-- Idempotent (ADD COLUMN IF NOT EXISTS). Existing rows get NULL page/method/
-- confidence (UI simply omits the source link) until a --force re-extract
-- backfills them — which replays from the content-addressed cache for $0 on
-- any document already extracted under the current extractor version.

-- ---- motion ----------------------------------------------------------------
ALTER TABLE motion
    ADD COLUMN IF NOT EXISTS source_page           SMALLINT;  -- 1-indexed PDF page where the item begins
ALTER TABLE motion
    ADD COLUMN IF NOT EXISTS extraction_method     TEXT;      -- 'text_layer','ocr','vision','cached','prebuilt','manual_entry'
ALTER TABLE motion
    ADD COLUMN IF NOT EXISTS extraction_confidence TEXT;
ALTER TABLE motion
    DROP CONSTRAINT IF EXISTS motion_extraction_confidence_ck;
ALTER TABLE motion
    ADD CONSTRAINT motion_extraction_confidence_ck
        CHECK (extraction_confidence IS NULL OR extraction_confidence IN ('high','medium','low'));

-- ---- vote ------------------------------------------------------------------
ALTER TABLE vote
    ADD COLUMN IF NOT EXISTS source_page           SMALLINT;  -- inherits its motion's page
ALTER TABLE vote
    ADD COLUMN IF NOT EXISTS attribution           TEXT NOT NULL DEFAULT 'recorded';
ALTER TABLE vote
    ADD COLUMN IF NOT EXISTS extraction_confidence TEXT;
ALTER TABLE vote
    DROP CONSTRAINT IF EXISTS vote_attribution_ck;
ALTER TABLE vote
    ADD CONSTRAINT vote_attribution_ck
        CHECK (attribution IN ('recorded','inferred'));
ALTER TABLE vote
    DROP CONSTRAINT IF EXISTS vote_extraction_confidence_ck;
ALTER TABLE vote
    ADD CONSTRAINT vote_extraction_confidence_ck
        CHECK (extraction_confidence IS NULL OR extraction_confidence IN ('high','medium','low'));
