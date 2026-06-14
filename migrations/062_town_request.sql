-- Town requests — "ask TownWatch to onboard my town".
--
-- During beta, towns are onboarded slowly, by request, by an operator (not
-- self-serve). An uncovered town's page shows a lock + a "request this town"
-- action; a signed-in resident's request lands here for the operator to review,
-- then fulfil by pressing the adopt seal (which writes an onboard_request).
--
-- Distinct from onboard_request (the founding intent + cadence queue): this is
-- the upstream demand signal. Web writes; the operator reads.

CREATE TABLE IF NOT EXISTS town_request (
    id               BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug             TEXT        NOT NULL,
    directory_fips   TEXT        NOT NULL,
    state_abbr       TEXT        NOT NULL,
    display_name     TEXT        NOT NULL,
    requester_user_id TEXT       NOT NULL,
    note             TEXT,
    status           TEXT        NOT NULL DEFAULT 'open',
    requested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One open request per (town, requester) — clicking twice doesn't stack, but a
-- request count across requesters is still the demand signal.
CREATE UNIQUE INDEX IF NOT EXISTS town_request_open_unique
    ON town_request (slug, requester_user_id)
    WHERE status = 'open';

CREATE INDEX IF NOT EXISTS town_request_slug_idx ON town_request (slug, status);

COMMENT ON TABLE town_request IS
    'Beta demand signal: residents asking TownWatch to onboard a town. The '
    'operator reviews and fulfils by pressing the adopt seal. Distinct from '
    'onboard_request (the founding intent the seal writes).';
