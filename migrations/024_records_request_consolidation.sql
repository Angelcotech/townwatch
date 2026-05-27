-- Migration 024: consolidated records requests.
--
-- A "consolidated" records request rolls all open findings for a
-- jurisdiction into one letter to the records custodian — one email
-- to Vicki Capetillo covering everything for Grovetown instead of
-- eight separate sends.
--
-- finding_ids stores every finding included in this request. The
-- existing finding_id column is preserved as the "primary" (the
-- first finding in the array) so older code keeps working while the
-- new consolidated path takes over.

ALTER TABLE records_request
    ADD COLUMN finding_ids INTEGER[];

-- Backfill existing rows: each historical per-finding request becomes
-- a single-item array. Future requests created via the consolidated
-- path will write the full multi-finding array directly.
UPDATE records_request
SET finding_ids = ARRAY[finding_id]
WHERE finding_id IS NOT NULL AND finding_ids IS NULL;

-- GIN index on the array for fast "is this finding covered by any
-- open request?" lookups during the auto-consolidation pass.
CREATE INDEX records_request_finding_ids_gin
    ON records_request USING GIN (finding_ids);
