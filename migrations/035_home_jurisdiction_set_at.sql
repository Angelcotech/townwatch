-- Migration 035: track when a user last set their home jurisdiction.
--
-- Standing (home city + its county) is self-declared, so a user must be able to
-- update it when they move — but not hop jurisdictions freely to comment
-- everywhere. A timestamp lets us enforce a change cooldown (30 days) while
-- leaving the first choice free. updated_at can't serve this (it moves on any
-- field change, e.g. the Clerk email sync).

ALTER TABLE app_user
    ADD COLUMN IF NOT EXISTS home_jurisdiction_set_at TIMESTAMPTZ;
