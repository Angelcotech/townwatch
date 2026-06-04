-- 040_onboarding_estimate.sql
--
-- Per-jurisdiction onboarding cost estimate — the forward-looking dollar figure
-- for fully indexing a jurisdiction's available agendas + minutes. This is the
-- funding GOAL shown on /adopt ("be the first to build this town for ~$X"),
-- distinct from the fund_ledger BALANCE (what's actually been contributed/spent).
--
-- Computed by jobs/estimate_onboarding.py from the cheap, already-scanned
-- meeting inventory: it counts the real (non-placeholder) agenda and minutes
-- documents a jurisdiction publishes and multiplies by an honest per-document
-- rate. The rate is EMPIRICAL — the rolling average of what real extractions
-- have actually cost (from fund_ledger spend rows) — and falls back to a
-- conservative default only until enough real spend exists to calibrate. So the
-- estimate is reconciled to real money, not a guess, and self-improves as the
-- system extracts more documents.
--
-- One row per jurisdiction, recomputed each pipeline pass (after inventory +
-- scan, before any paid extraction). No spend involved — pure SQL over data we
-- already have. The basis/meta columns make the estimate auditable: a reader can
-- see exactly how many documents of each kind and which rate produced the goal.

CREATE TABLE IF NOT EXISTS jurisdiction_onboarding_estimate (
    jurisdiction_id     INTEGER PRIMARY KEY REFERENCES jurisdiction(id) ON DELETE CASCADE,

    -- Workload counts (real, non-placeholder documents only).
    agenda_documents    INTEGER NOT NULL DEFAULT 0,
    minutes_documents   INTEGER NOT NULL DEFAULT 0,
    documents_total     INTEGER NOT NULL DEFAULT 0,   -- full indexing workload
    documents_remaining INTEGER NOT NULL DEFAULT 0,   -- not yet extracted

    -- Dollars. estimate_usd = cost to index everything (the funding goal);
    -- remaining_usd = cost of the not-yet-done remainder (what new funds buy).
    estimate_usd        NUMERIC(14,6) NOT NULL DEFAULT 0,
    remaining_usd       NUMERIC(14,6) NOT NULL DEFAULT 0,

    -- How the rate was derived: 'empirical' (calibrated from real spend),
    -- 'default' (conservative pre-data constant), or 'mixed' (one kind each).
    basis               TEXT NOT NULL DEFAULT 'default',

    -- Audit trail: per-kind rates, doc counts, sample size behind the rate.
    meta                JSONB NOT NULL DEFAULT '{}'::jsonb,

    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE jurisdiction_onboarding_estimate IS
    'Forward-looking onboarding cost estimate per jurisdiction (the /adopt '
    'funding goal). Recomputed each pipeline pass by jobs/estimate_onboarding.py '
    'from non-placeholder agenda/minutes counts x an empirical per-document rate '
    'drawn from fund_ledger spend. Distinct from the fund_ledger balance.';
