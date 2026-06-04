-- 041_activity_log.sql
--
-- TownWatch's own action history — a per-jurisdiction (and org-wide) timeline of
-- the meaningful things TownWatch DID: a town was added, a build phase finished,
-- a records request was generated/sent, a public-comment digest was submitted to
-- the clerk, funding was received. Powers a per-jurisdiction Activity tab AND a
-- global "watch TownWatch work" live stream (Adopt-a-Town).
--
-- Milestones only — routine per-document extraction is NOT logged here (it would
-- bury the signal). Append-only and written through ONE module (activity.py),
-- mirroring the fund_ledger discipline: a single writer, ref_kind/ref_id pointers
-- to the thing that happened, and a JSONB meta for detail.
--
-- jurisdiction_id is NULL for org-level events (e.g. a state legal catalog being
-- certified). actor_user_id reserves first-adopter / contributor attribution for
-- when the payment rail lands; today the genesis event is the system "added".
--
-- Idempotency: once-only milestones (a phase first indexed, a town first added)
-- set dedupe_key; a partial UNIQUE index makes re-emitting them a no-op. Ordinary
-- events leave dedupe_key NULL and never collide.

CREATE TABLE IF NOT EXISTS activity_log (
    id              BIGSERIAL   PRIMARY KEY,
    jurisdiction_id INTEGER     REFERENCES jurisdiction (id) ON DELETE CASCADE,  -- NULL = org-level
    action_type     TEXT        NOT NULL,   -- 'jurisdiction_added','phase_indexed','records_request_generated','comments_submitted','funding_received',...
    title           TEXT        NOT NULL,   -- human one-liner for the timeline
    ref_kind        TEXT,                   -- 'capability','records_request','meeting','ledger',...
    ref_id          TEXT,                   -- the referenced row's id/key
    meta            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    actor_user_id   BIGINT      REFERENCES app_user (id),  -- contributor/first-adopter, when known
    dedupe_key      TEXT,                   -- set for once-only milestones; NULL otherwise
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_activity_log_juris_time ON activity_log (jurisdiction_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_log_time ON activity_log (occurred_at DESC);

-- Once-only milestones dedupe on (jurisdiction_id, dedupe_key). Partial so
-- ordinary events (dedupe_key NULL) are exempt and can repeat freely.
CREATE UNIQUE INDEX IF NOT EXISTS uq_activity_log_dedupe
    ON activity_log (jurisdiction_id, dedupe_key) WHERE dedupe_key IS NOT NULL;

COMMENT ON TABLE activity_log IS
    'Append-only milestone history of TownWatch actions per jurisdiction (NULL = '
    'org-level). Written only by activity.py. Powers the Activity tab + global '
    'live event stream. Routine per-document extraction is intentionally excluded.';
