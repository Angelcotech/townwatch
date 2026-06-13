-- document_text_url — every source URL ever resolved to stored content.
--
-- document_text is content-addressed (PK = content_hash), with a single
-- informational source_url column. Many URLs can serve identical bytes (reused
-- stub agendas, the same packet at two meeting URLs), but only ONE wins that
-- column. backfill_document_text._already_stored filtered by source_url, so any
-- byte-duplicate under a different URL was never marked seen — it re-fetched and
-- re-extracted on every daily run, permanently occupying queue slots under the
-- --limit cap (e.g. Columbia County fileId=997, a 1-page agenda, stored every
-- run yet never dequeued). Found during 2026-06-13 triage.
--
-- This table records EVERY processed URL → the content it resolved to, so the
-- backfill skip-set is URL-complete. Many URLs → one content_hash.

CREATE TABLE IF NOT EXISTS document_text_url (
    source_url    TEXT        PRIMARY KEY,
    content_hash  TEXT        NOT NULL REFERENCES document_text (content_hash) ON DELETE CASCADE,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS document_text_url_hash_idx
    ON document_text_url (content_hash);

COMMENT ON TABLE document_text_url IS
    'Every source URL ever resolved to stored document_text content (many URLs '
    'per content_hash). Written by document_text.get_or_recover on every call; '
    'read by backfill_document_text._already_stored so byte-duplicate documents '
    'under different URLs are not re-processed every run.';

-- Backfill from the URLs already recorded on document_text rows.
INSERT INTO document_text_url (source_url, content_hash)
SELECT source_url, content_hash
FROM document_text
WHERE source_url IS NOT NULL
ON CONFLICT (source_url) DO NOTHING;
