-- Onboard requests — the founder ledger + the cadence queue for adopting a town.
--
-- An operator presses the wax seal on an uncovered town's funding page; the web
-- records the intent here (who founded it, when, their place in line). The ETL's
-- process_onboard_requests job consumes pending rows on its cadence, runs
-- scaffold for the town, and credits the founder in the genesis activity event
-- ("Founded by <name>"). Same web-writes-intent / ETL-does-the-work split as the
-- rest of the pipeline — this row is the hand-off.
--
-- founder_number is the founder's ordinal (Grovetown = 1, the next town = 2, …),
-- computed by the web at press time so the confirmation can show it and the
-- record keeps it. status: pending → done (or failed / awaiting_config).

CREATE TABLE IF NOT EXISTS onboard_request (
    id              BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug            TEXT        NOT NULL,
    directory_fips  TEXT        NOT NULL,
    state_abbr      TEXT        NOT NULL,
    display_name    TEXT        NOT NULL,
    founder_user_id TEXT        NOT NULL,
    founder_name    TEXT,
    founder_number  INTEGER,
    status          TEXT        NOT NULL DEFAULT 'pending',
    error           TEXT,
    jurisdiction_id BIGINT      REFERENCES jurisdiction (id) ON DELETE SET NULL,
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at    TIMESTAMPTZ
);

-- One active request per town — can't double-adopt a place that's pending or done.
-- A failed request can be retried (it's excluded from the uniqueness arbiter).
CREATE UNIQUE INDEX IF NOT EXISTS onboard_request_active_slug
    ON onboard_request (slug)
    WHERE status IN ('pending', 'done', 'awaiting_config');

-- The ETL consumer scans for work by status.
CREATE INDEX IF NOT EXISTS onboard_request_status_idx
    ON onboard_request (status, requested_at);

COMMENT ON TABLE onboard_request IS
    'Founder ledger + cadence onboard queue. Web inserts the founding intent when '
    'an operator presses the adopt seal; process_onboard_requests (ETL) runs '
    'scaffold and credits the founder in the genesis activity event.';
