-- Migration 017: meeting.agenda_posted_at
-- Captures the timestamp the agenda was actually uploaded to the city's
-- agenda system (CivicEngage publishes "Posted Mar 6, 2026 12:27 PM"
-- alongside each row). The diff between agenda_posted_at and
-- meeting_date is the citizen-notice signal — independent of whether
-- the city is meeting the legal minimum.

ALTER TABLE meeting
    ADD COLUMN IF NOT EXISTS agenda_posted_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_meeting_agenda_posted_at ON meeting (agenda_posted_at)
    WHERE agenda_posted_at IS NOT NULL;

-- The notice-gap (meeting_date - agenda_posted_at::date) is computed at
-- query time by the observe_meeting_notice_too_short observer. We don't
-- index the expression because ::date on a TIMESTAMPTZ isn't IMMUTABLE
-- in PostgreSQL's functional-index sense, and the population of meetings
-- per-jurisdiction (~hundreds) is small enough that a sequential filter
-- on agenda_posted_at-indexed rows is fast.
