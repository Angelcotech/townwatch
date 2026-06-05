-- 044_clerk_contact_health.sql
--
-- Health of the records-custodian (clerk) email — the single address that records
-- requests and public-comment digests are delivered to. If it goes stale, changes,
-- or bounces, the whole active arm fails SILENTLY (requests/digests never arrive).
-- This tracks its standing so a broken contact surfaces instead of disappearing.
--
-- Status values (worst-wins precedence when monitor + delivery disagree):
--   verified      -- syntactically valid + domain resolves; last send (if any) ok
--   unverified    -- malformed, dead domain, or no email on file
--   changed       -- the published clerk email appears to differ from ours (review,
--                    never auto-overwrite — a bad re-extraction could corrupt it)
--   undeliverable -- a real send to this address failed (the hardest signal)
-- NULL = never checked yet. Maintained by jobs/monitor_clerk_contact.py and flagged
-- to the pipeline_failure admin queue; web surfaces a "contact needs review" hint.

ALTER TABLE jurisdiction
    ADD COLUMN IF NOT EXISTS records_custodian_email_status TEXT,
    ADD COLUMN IF NOT EXISTS records_custodian_email_checked_at TIMESTAMPTZ;

COMMENT ON COLUMN jurisdiction.records_custodian_email_status IS
    'Deliverability standing of the clerk/records-custodian email: verified | '
    'unverified | changed | undeliverable | NULL(unchecked). Set by '
    'monitor_clerk_contact.py + submit_comments delivery outcomes.';
