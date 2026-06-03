-- Migration 037: agenda packet — the actual proposal documents.
--
-- The agenda is a docket/summary; the agenda PACKET is the combined PDF holding
-- the agenda plus every supporting document (staff reports, applications,
-- exhibits) — the actual proposal a resident should read before commenting.
--
-- meeting.packet_url is the packet PDF (captured by the scraper). For each
-- agenda item we store the page range in the packet where ITS materials live,
-- plus an AI summary of the ACTUAL document (what it proposes / what staff
-- recommends) — distinct from the agenda's one-line description. The forum and
-- the meeting section deep-link "the full proposal · pp. X–Y" and show this.

ALTER TABLE meeting
    ADD COLUMN IF NOT EXISTS packet_url TEXT;

ALTER TABLE agenda_item
    ADD COLUMN IF NOT EXISTS packet_start_page SMALLINT;   -- 1-indexed page in the packet
ALTER TABLE agenda_item
    ADD COLUMN IF NOT EXISTS packet_end_page   SMALLINT;
ALTER TABLE agenda_item
    ADD COLUMN IF NOT EXISTS proposal_summary  TEXT;       -- AI summary of the ACTUAL document
ALTER TABLE agenda_item
    ADD COLUMN IF NOT EXISTS packet_segmented_at TIMESTAMPTZ;  -- when segmentation last ran
