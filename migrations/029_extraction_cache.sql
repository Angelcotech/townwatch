-- Migration 029: extraction_cache — never pay twice for the same document.
--
-- Extraction output was already saved (data_source.raw_payload) but never read
-- back, so every re-run re-called the model and re-spent. This table makes
-- extraction content-addressed: keyed by (hash of the source document bytes,
-- doc kind, extractor version). A re-run hashes the freshly-downloaded document,
-- finds the cached result, and replays it into the DB for $0 — the model is
-- only called when the document is genuinely new or the extractor version was
-- deliberately bumped.
--
-- Invalidation policy (chosen): re-extract (re-pay) only when
--   * the source document bytes change (→ new content_hash), OR
--   * extractor_version is bumped (improved prompt/schema/model) — a version
--     bump leaves old rows in place but no longer matches, so the next process
--     re-extracts under the new version and writes a new cache row.
-- Old-version rows are kept (audit / rollback), not deleted.
--
-- This is the precondition for donor-funded processing: a contributor pays once
-- per document, ever — never for a re-run, a resume, or an outage restart.

CREATE TABLE IF NOT EXISTS extraction_cache (
    id                 BIGSERIAL PRIMARY KEY,
    content_hash       TEXT NOT NULL,            -- sha256 hex of the source document bytes
    doc_kind           TEXT NOT NULL,            -- 'minutes' | 'agenda' | ...
    extractor_version  TEXT NOT NULL,            -- bumped when prompt/schema/model changes
    source_url         TEXT,                     -- provenance (informational)
    method             TEXT,                     -- original modality: text_layer | ocr | vision
    extraction         JSONB NOT NULL,           -- the full extraction JSON (MeetingExtraction)
    cost_usd           NUMERIC(14,6),            -- what producing it originally cost (reporting)
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One canonical cached result per (document, kind, version).
    UNIQUE (content_hash, doc_kind, extractor_version)
);

CREATE INDEX IF NOT EXISTS extraction_cache_lookup_idx
    ON extraction_cache (content_hash, doc_kind, extractor_version);
