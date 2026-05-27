-- Migration 022: records-custodian contact on jurisdiction row.
--
-- The custodian (city clerk, county clerk, etc.) is the citizen-facing
-- address for open-records requests. Currently their email + name +
-- title live only in the per-jurisdiction config file; the admin
-- portal needs them at query time to populate the mailto: compose
-- link without reading config files at request time.
--
-- Synced from configs by the extended sync_jurisdictions job. Body-
-- level custodian overrides (rare — see _jurisdiction.schema.json's
-- governing_body.records_custodian) are not denormalized here yet;
-- they're picked up separately when prepare_records_request reads
-- the config directly.

ALTER TABLE jurisdiction
    ADD COLUMN records_custodian_name  TEXT,
    ADD COLUMN records_custodian_title TEXT,
    ADD COLUMN records_custodian_email TEXT;
