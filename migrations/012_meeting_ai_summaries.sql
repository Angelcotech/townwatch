-- Migration 012: per-document AI summaries on meeting
-- Two columns hold short plain-English summaries written by Claude during
-- agenda / minutes extraction. The pair lets readers see the request side
-- (agenda) and the action side (minutes) without opening the source PDFs.

ALTER TABLE meeting
    ADD COLUMN agenda_ai_summary  TEXT,
    ADD COLUMN minutes_ai_summary TEXT;
