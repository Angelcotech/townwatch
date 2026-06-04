-- 042_jurisdiction_capability.sql
--
-- Persisted build-capability state per jurisdiction — the source of truth for the
-- build-progress ladder (directory, meetings, minutes/votes, roster, audit,
-- campaign finance, elections, budget). Until now the ladder was recomputed live
-- in the web layer with no memory of WHEN a phase finished; this table records
-- the current state plus first_indexed_at, so "Meetings indexed — Mar 2026" can
-- show on the dashboard and a capability finishing emits an activity milestone.
--
-- Maintained by jobs/sync_capabilities.py (cheap SQL, runs in scaffold + at the
-- tail of daily_refresh). The web getBuildProgress reads this instead of
-- recomputing, removing the live/ETL threshold-drift risk. State values mirror
-- the web CapabilityState union: indexed | in_progress | needs_funding |
-- coming_soon.

CREATE TABLE IF NOT EXISTS jurisdiction_capability (
    jurisdiction_id   INTEGER     NOT NULL REFERENCES jurisdiction (id) ON DELETE CASCADE,
    capability_key    TEXT        NOT NULL,   -- 'directory','meetings','minutes','roster','audit',...
    state             TEXT        NOT NULL,   -- indexed | in_progress | needs_funding | coming_soon
    first_indexed_at  TIMESTAMPTZ,            -- when it first reached 'indexed' (NULL until then)
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (jurisdiction_id, capability_key)
);

COMMENT ON TABLE jurisdiction_capability IS
    'Per-jurisdiction build-capability state + first_indexed_at. Written by '
    'jobs/sync_capabilities.py; read by the web build-progress widget. '
    'Transitions into indexed emit a phase_indexed activity milestone.';
