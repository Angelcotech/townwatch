-- Migration 034: accounts + public comment + operating reserve.
--
-- Introduces TownWatch's first user-identity and public-input layer, plus a
-- funding reserve that keeps a built town's essential operations alive.
--
-- 1. app_user — a mirror of the Clerk identity (auth lives in Clerk; the
--    relationship + standing live here so the data is ours and the web stays
--    read-only). Carries a self-declared home jurisdiction, which is the
--    constituent-standing gate for commenting.
--
-- 2. public_comment — a constrained public comment: one structured submission
--    per (user, agenda item), with a Support/Oppose/Neutral stance, AI-moderated
--    before it goes live. NOT a forum — no threads, no replies. Comments are
--    never deleted; once a meeting's comment window closes they become the
--    permanent record, published alongside the agenda + minutes.
--
-- 3. jurisdiction_fund.operating_reserve — a protected balance band. Essential
--    recurring ops (daily-refresh extraction of newly-found meetings, comment
--    moderation) may draw down to the hard min_balance_floor; discretionary
--    work (--force re-extraction, --backfill-*) is declined once the balance
--    dips into floor + operating_reserve, so a built town can always stay
--    current and keep moderating comments. (Enforcement lives in funds.gate.)
--
-- Idempotent (IF NOT EXISTS throughout).

-- ---- accounts --------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_user (
    id                    BIGSERIAL PRIMARY KEY,
    clerk_user_id         TEXT NOT NULL UNIQUE,          -- the Clerk user id (front door)
    email                 TEXT,
    display_name          TEXT,
    -- Self-declared home jurisdiction — the standing anchor. A user may comment
    -- only on their home city + that city's parent county.
    home_jurisdiction_id  INTEGER REFERENCES jurisdiction (id),
    status                TEXT NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active', 'blocked')),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS app_user_home_jurisdiction_idx
    ON app_user (home_jurisdiction_id);

-- ---- public comment --------------------------------------------------------
CREATE TABLE IF NOT EXISTS public_comment (
    id                BIGSERIAL PRIMARY KEY,
    app_user_id       BIGINT  NOT NULL REFERENCES app_user (id),
    agenda_item_id    INTEGER NOT NULL REFERENCES agenda_item (id),
    -- Denormalized for tally/window/record queries (avoids a join to agenda_item
    -- on every meeting render).
    meeting_id        INTEGER NOT NULL REFERENCES meeting (id),
    stance            TEXT NOT NULL CHECK (stance IN ('support', 'oppose', 'neutral')),
    body              TEXT NOT NULL,
    -- 'pending'   — submitted, awaiting moderation
    -- 'published' — passed moderation, visible + counted in the tally
    -- 'rejected'  — failed moderation (kept for audit, never shown)
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'published', 'rejected')),
    moderation_reason TEXT,
    reporter_ip       INET,
    moderated_at      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at      TIMESTAMPTZ,
    -- One comment per person per agenda item (anti-flood; you speak once).
    UNIQUE (app_user_id, agenda_item_id)
);

CREATE INDEX IF NOT EXISTS public_comment_item_status_idx
    ON public_comment (agenda_item_id, status);
CREATE INDEX IF NOT EXISTS public_comment_meeting_idx
    ON public_comment (meeting_id);
CREATE INDEX IF NOT EXISTS public_comment_user_idx
    ON public_comment (app_user_id);

-- ---- operating reserve -----------------------------------------------------
ALTER TABLE jurisdiction_fund
    ADD COLUMN IF NOT EXISTS operating_reserve NUMERIC(14,6) NOT NULL DEFAULT 0;
