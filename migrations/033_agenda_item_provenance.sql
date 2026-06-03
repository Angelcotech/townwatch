-- Migration 033: extraction honesty on agenda_item.
--
-- agenda_item already carries source_page (migration 011), so an agenda datum
-- is page-traceable. This brings it to parity with motion/vote (migration 031)
-- by adding how it was read and how confident the extractor was — so the agenda
-- side of the record is just as honestly qualified as the minutes side.
--
-- Idempotent (ADD COLUMN IF NOT EXISTS). Existing rows get NULL until a
-- --backfill-provenance pass replays their stored extraction for $0.

ALTER TABLE agenda_item
    ADD COLUMN IF NOT EXISTS extraction_method     TEXT;  -- 'text_layer','ocr','vision','docx','doc','cached','prebuilt'
ALTER TABLE agenda_item
    ADD COLUMN IF NOT EXISTS extraction_confidence TEXT;
ALTER TABLE agenda_item
    DROP CONSTRAINT IF EXISTS agenda_item_extraction_confidence_ck;
ALTER TABLE agenda_item
    ADD CONSTRAINT agenda_item_extraction_confidence_ck
        CHECK (extraction_confidence IS NULL OR extraction_confidence IN ('high','medium','low'));
