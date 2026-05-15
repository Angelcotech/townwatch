-- Migration 010: official.is_active
--
-- Distinguishes currently-serving officials (on the city's roster /
-- staff directory today) from former ones (still in the index because
-- they appear in historical motions). Mirrors how term.is_current
-- already works for elected officials; this gives staff the same
-- distinction without adding a parallel "staff_term" table.

ALTER TABLE official
    ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE;

CREATE INDEX idx_official_is_active ON official(is_active);
