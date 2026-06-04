-- 039_jurisdiction_timezone_status.sql
--
-- Confidence flag for jurisdiction.timezone, so onboarding never has to choose
-- between "block the town" and "onboard it with a silently-wrong clock".
--
--   verified — the zone is certain: an explicit config override, a county-level
--              match, or a single-zone state.
--   assumed  — a best guess: a multi-zone state's predominant zone where we
--              couldn't pin the county, or the ultimate default. The town still
--              onboards and its forum still works; this just marks it for
--              troubleshoot_timezones to surface for a one-line confirmation.
--
-- Resolved alongside jurisdiction.timezone by sync_jurisdictions. Nullable;
-- a NULL simply means "not yet resolved" and reads as needing review.

ALTER TABLE jurisdiction
    ADD COLUMN IF NOT EXISTS timezone_status TEXT
        CHECK (timezone_status IN ('verified', 'assumed'));

COMMENT ON COLUMN jurisdiction.timezone_status IS
    'Confidence in jurisdiction.timezone: verified (explicit/county/single-zone) '
    'or assumed (multi-zone predominant guess or default). Set by '
    'sync_jurisdictions; assumed rows are listed by troubleshoot_timezones.';
