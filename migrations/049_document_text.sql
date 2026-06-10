-- Content-addressed READABLE TEXT for every document we process.
--
-- Until now we OCR'd a scanned document (Mistral), fed the text straight to the
-- model to produce a STRUCTURED extraction, and discarded the text. So every new
-- consumer (e.g. budget extraction reading an agenda we'd already OCR'd) had to
-- re-scan. This table keeps the recovered text once, keyed by the document bytes,
-- so it's reused across every extractor and stays available for future uses
-- (e.g. embedding/RAG for a TownWatch chat agent — embeddings are derived from
-- THIS, computed later under whatever model is best then).
--
-- One canonical text per document (content_hash). A better recovery (e.g. vision)
-- upserts in place. Pages are stored separately so page-range consumers (packets,
-- budgets) work without re-splitting.

CREATE TABLE IF NOT EXISTS document_text (
    content_hash TEXT PRIMARY KEY,        -- sha256 hex of the source document bytes
    source_url   TEXT,                    -- provenance (informational)
    method       TEXT,                    -- text_layer | ocr | none | not_pdf
    page_count   INTEGER,
    pages        JSONB NOT NULL,          -- array of per-page text strings
    char_count   INTEGER,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
