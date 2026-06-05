-- 045_clerk_contact_observed.sql
--
-- Drift detection for the records-custodian (clerk) email. monitor_clerk_contact
-- can re-read the published email from a configured contact-source page and
-- compare it to the one we hold. When the page no longer lists our address but
-- DOES list a different clerk/records-looking address on the same domain, we set
-- status 'changed' and record what the page now shows here — so the operator can
-- see the candidate replacement and update the config. We NEVER auto-overwrite the
-- stored email (a bad re-extraction could corrupt the one channel that reaches the
-- clerk); this column is review data, not the source of truth.

ALTER TABLE jurisdiction
    ADD COLUMN IF NOT EXISTS records_custodian_email_observed TEXT;

COMMENT ON COLUMN jurisdiction.records_custodian_email_observed IS
    'The clerk/records email most recently OBSERVED on the contact-source page when '
    'it differs from records_custodian_email (drift candidate, operator review only). '
    'Never auto-applied. Set by monitor_clerk_contact.py.';
