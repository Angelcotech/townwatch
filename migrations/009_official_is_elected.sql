-- Migration 009: official.is_elected
--
-- Distinguishes elected officials (mayor, council) from appointed staff
-- (city administrator, attorney, dept directors). Both share the same
-- table — same identity resolution, photos, bios, profiles — but staff
-- by definition won't have a `term` row, won't have `vote` rows, and
-- the qa_orphan_official detector must skip them.
--
-- All existing rows default to TRUE for safety: today's officials in the
-- DB are all elected.

ALTER TABLE official
    ADD COLUMN is_elected BOOLEAN NOT NULL DEFAULT TRUE;

CREATE INDEX idx_official_is_elected ON official(is_elected);
