-- Migration 025: meeting.agenda_is_placeholder / minutes_is_placeholder
--
-- Civic platforms (CivicEngage today, likely others tomorrow) serve a
-- tiny blank PDF for meetings whose document was never actually uploaded
-- — valid PDF header, zero content, ~1.5–2KB. The extractor already
-- short-circuits on these (see extractors/agendas.py:_stub_extraction);
-- this column persists that signal on the meeting row so the frontend
-- can render "placeholder served by city" instead of a dead download
-- link. The audit reframe applies: absence of a real document is itself
-- a finding, not noise to be hidden.
--
-- Both columns ship together so the template treats agendas and minutes
-- symmetrically. Minutes-stub detection isn't wired in the extractor
-- yet — column defaults to false until that lands.

ALTER TABLE meeting
    ADD COLUMN IF NOT EXISTS agenda_is_placeholder  BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS minutes_is_placeholder BOOLEAN NOT NULL DEFAULT false;

-- Backfill agenda_is_placeholder for already-extracted rows. The
-- extractor wrote 'stub_skipped: ...' into raw_payload.extraction_notes
-- whenever it recognised a placeholder; that's a durable signal we can
-- replay without re-downloading PDFs.
UPDATE meeting m
SET agenda_is_placeholder = true
FROM data_source ds
WHERE ds.record_url = m.agenda_url
  AND ds.source_name LIKE '%AgendaCenter:claude_extract%'
  AND ds.raw_payload->>'extraction_notes' LIKE 'stub_skipped:%'
  AND m.agenda_is_placeholder = false;
