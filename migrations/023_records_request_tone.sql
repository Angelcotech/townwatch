-- Migration 023: tone level on records_request.
--
-- Records requests start friendly (1) and escalate manually as needed
-- via an operator button on the admin queue:
--   1 = friendly  — mission-led, gratitude, disarming intro before the ask
--   2 = standard  — business-formal follow-up, statute referenced
--   3 = strict    — full legal demand with quoted statute + deadlines
--
-- The PDF + the mailto: email body both respect this column. Escalating
-- triggers a regenerate of the PDF at the new tone. Default is 1 so
-- every NEW request leads with the friendly approach.

ALTER TABLE records_request
    ADD COLUMN tone SMALLINT NOT NULL DEFAULT 1
        CHECK (tone IN (1, 2, 3));
