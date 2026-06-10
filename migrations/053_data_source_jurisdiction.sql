-- Migration 053: data_source.jurisdiction_id (provenance becomes jurisdiction-aware)
--
-- data_source is the per-ingest-run provenance log. It was keyed only by source
-- (scraper) name — at 3 jurisdictions you can tell them apart because the host is
-- baked into source_name, but at scale many towns share one platform (CivicClerk,
-- BoardDocs, Granicus), so the name won't partition by jurisdiction and
-- "everything ingested for town X" means joining out through content rows. This
-- adds a direct, nullable jurisdiction_id (statewide/manual sources stay NULL).
--
-- Backfill resolves each existing row through the content it produced (a run is
-- per-jurisdiction, so any referencing row's jurisdiction is THE jurisdiction).
-- Going forward, daily_refresh re-runs the same NULL-only reconcile so new rows
-- become jurisdiction-aware without threading jid through every ingest job.

ALTER TABLE data_source ADD COLUMN IF NOT EXISTS jurisdiction_id INTEGER
    REFERENCES jurisdiction (id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_data_source_jurisdiction ON data_source (jurisdiction_id);

-- FK indexes the audit found missing on the high-volume content tables. They make
-- the content-trace backfill/reconcile fast and answer "all content from run N".
CREATE INDEX IF NOT EXISTS idx_meeting_data_source     ON meeting (data_source_id);
CREATE INDEX IF NOT EXISTS idx_agenda_item_data_source ON agenda_item (data_source_id);
CREATE INDEX IF NOT EXISTS idx_motion_data_source      ON motion (data_source_id);

-- Content-trace backfill. DISTINCT ON picks one jurisdiction per source (a run
-- maps to one), only fills NULLs so it's safe to re-run.
WITH ds_juris AS (
    SELECT DISTINCT ON (data_source_id) data_source_id, jurisdiction_id
    FROM (
        SELECT m.data_source_id, gb.jurisdiction_id
          FROM meeting m JOIN governing_body gb ON gb.id = m.governing_body_id
         WHERE m.data_source_id IS NOT NULL
        UNION ALL
        SELECT ai.data_source_id, gb.jurisdiction_id
          FROM agenda_item ai JOIN meeting m ON m.id = ai.meeting_id
          JOIN governing_body gb ON gb.id = m.governing_body_id
         WHERE ai.data_source_id IS NOT NULL
        UNION ALL
        SELECT mo.data_source_id, gb.jurisdiction_id
          FROM motion mo JOIN meeting m ON m.id = mo.meeting_id
          JOIN governing_body gb ON gb.id = m.governing_body_id
         WHERE mo.data_source_id IS NOT NULL
        UNION ALL
        SELECT s.data_source_id, gb.jurisdiction_id
          FROM seat s JOIN governing_body gb ON gb.id = s.governing_body_id
         WHERE s.data_source_id IS NOT NULL
        UNION ALL
        SELECT t.data_source_id, gb.jurisdiction_id
          FROM term t JOIN seat s ON s.id = t.seat_id
          JOIN governing_body gb ON gb.id = s.governing_body_id
         WHERE t.data_source_id IS NOT NULL
        UNION ALL
        SELECT gb.data_source_id, gb.jurisdiction_id
          FROM governing_body gb WHERE gb.data_source_id IS NOT NULL
        UNION ALL
        SELECT j.data_source_id, j.id
          FROM jurisdiction j WHERE j.data_source_id IS NOT NULL
    ) refs
    WHERE jurisdiction_id IS NOT NULL
    ORDER BY data_source_id
)
UPDATE data_source ds SET jurisdiction_id = dj.jurisdiction_id
FROM ds_juris dj
WHERE ds.id = dj.data_source_id AND ds.jurisdiction_id IS NULL;
