-- Migration 036: comment digest delivery to officials.
--
-- The live forum closes 12 hours before a meeting; at that point TownWatch
-- compiles the published public comments per agenda item, an AI agent reviews
-- the compiled digest one last time, and (if cleared) it's emailed to the
-- records custodian who runs the meeting. comments_submitted_at marks that the
-- packet was finalized — it both closes the comment window and prevents a
-- double-send. comment_digest is the audit record of what was sent (or held).

ALTER TABLE meeting
    ADD COLUMN IF NOT EXISTS comments_submitted_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS comment_digest (
    id               BIGSERIAL PRIMARY KEY,
    meeting_id       INTEGER NOT NULL REFERENCES meeting (id),
    recipient_email  TEXT,
    recipient_name   TEXT,
    item_count       INTEGER NOT NULL DEFAULT 0,
    comment_count    INTEGER NOT NULL DEFAULT 0,
    -- AI agent's final review of the compiled digest before it goes out.
    agent_decision   TEXT CHECK (agent_decision IN ('cleared', 'held')),
    agent_note       TEXT,
    body             TEXT,          -- the compiled digest text, for audit
    -- 'sent'   — emailed to the custodian
    -- 'held'   — agent flagged it; awaiting operator review, not sent
    -- 'no_recipient' — no custodian email on file; nothing to send
    -- 'failed' — send attempted but errored
    status           TEXT NOT NULL DEFAULT 'held'
                     CHECK (status IN ('sent', 'held', 'no_recipient', 'failed')),
    sent_at          TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS comment_digest_meeting_idx ON comment_digest (meeting_id);
