-- Migration 026: per-URL document-availability scan timestamps.
--
-- scan_document_availability previously re-checked EVERY agenda/minutes
-- URL on every run. Against a rate-limiting platform (CivicClerk) that is
-- O(all-history) work — it doesn't fit the daily window once we onboard
-- more than a handful of jurisdictions, and it needlessly re-hammers URLs
-- we already have a confident verdict on.
--
-- These columns let the scanner run INCREMENTALLY: record when each URL
-- was last given a *definitive* verdict (available / placeholder), then on
-- the next run skip URLs whose verdict is still fresh and only re-check
-- the ones that are due (never-scanned, or past their recheck interval).
-- Inconclusive verdicts (throttled / 5xx / network) deliberately do NOT
-- stamp these columns, so a throttle storm leaves a URL due for retry
-- rather than marking it "checked".
--
-- Per-kind (agenda vs minutes) because the two URLs resolve independently:
-- an agenda can come back 200 while the minutes URL is still throttled.
--
-- Guarded with IF NOT EXISTS so the migration is idempotent on replay —
-- the unguarded DDL in 019-024 is what desynced schema_migrations from the
-- live schema; new migrations should never repeat that.

ALTER TABLE meeting
    ADD COLUMN IF NOT EXISTS agenda_scanned_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS minutes_scanned_at TIMESTAMPTZ;
