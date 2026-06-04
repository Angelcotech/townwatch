-- 043_jurisdiction_capability_detail.sql
--
-- Store the human detail string ("12 meetings", "5 officials", "coming soon")
-- alongside each capability state, so the web build-progress widget reads
-- everything it renders straight from jurisdiction_capability — state, detail,
-- and first_indexed_at — instead of recomputing counts. Keeps the widget a dumb
-- reader and the ETL the single source of truth. Written by jobs/sync_capabilities.py.

ALTER TABLE jurisdiction_capability
    ADD COLUMN IF NOT EXISTS detail TEXT;
