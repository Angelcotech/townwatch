-- 038_jurisdiction_timezone.sql
--
-- Per-jurisdiction IANA time zone. Forum windows (the −12h comment cutoff and
-- the "live now" test) are computed from a meeting's local wall-clock date/time;
-- until now every surface interpreted that wall-clock as UTC, which skews the
-- real cutoff by the jurisdiction's offset (4–5h for Georgia). That was only
-- ever safe because every onboarded town happened to be Eastern.
--
-- The fix is template-grade: each jurisdiction carries its own IANA zone
-- (e.g. 'America/New_York', 'America/Chicago'), resolved automatically at
-- onboarding by sync_jurisdictions (state_fips → zone, with a per-jurisdiction
-- config override for the handful of states that span two zones). Every cutoff
-- query then reads `... AT TIME ZONE j.timezone` instead of a hard-coded 'UTC'.
--
-- Nullable here; sync_jurisdictions backfills + maintains it. Cutoff queries
-- COALESCE to 'America/New_York' purely as belt-and-suspenders for a row the
-- resolver hasn't touched yet.

ALTER TABLE jurisdiction
    ADD COLUMN IF NOT EXISTS timezone TEXT;

COMMENT ON COLUMN jurisdiction.timezone IS
    'IANA time zone (e.g. America/New_York). Resolved at onboarding by '
    'sync_jurisdictions from state_fips, overridable per-jurisdiction in config. '
    'Forum −12h cutoff math interprets meeting wall-clock times in this zone.';
