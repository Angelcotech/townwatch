-- Migration 028: per-jurisdiction funds — the donor↔spend bridge.
--
-- TownWatch processing costs real money (Anthropic tokens, Mistral OCR pages).
-- To scale to many jurisdictions on a shared pool WITHOUT one town's backfill
-- silently draining the whole budget, each jurisdiction carries its own funded
-- balance. Its own processing draws that balance down; when it nears a floor,
-- THAT jurisdiction pauses while every other one keeps running. This is a
-- runaway-spend circuit breaker + accounting layer, NOT an access gate — it
-- never decides who gets covered, only that spend can't exceed what was funded.
--
-- Accounting model: reserve-then-settle (authorization/capture), the model
-- payment systems use, chosen so it stays correct under parallel multi-worker /
-- multi-jurisdiction execution (the naive "deduct after spending" races to
-- negative when two units both see "enough" and both spend):
--
--   available(j) = SUM(fund_ledger.amount_usd)        -- deposits(+) − settled spend(−)
--                − SUM(fund_reservation.amount_usd)    -- open holds for in-flight work
--
--   * RESERVE expected cost before a unit runs (a hold). If
--     available − expected < floor → pause the jurisdiction, don't start.
--   * SETTLE to the ACTUAL cost (from real token/page usage) when it finishes:
--     append a ledger spend row, delete the reservation. Settlement reflects
--     real usage whether the unit succeeded or failed, so partial spend on a
--     failed extraction is still charged honestly; a zero-usage failure costs $0.
--
-- fund_ledger is append-only and immutable — the auditable source of truth
-- ("every penny reconciled"). fund_reservation is transient working state
-- (rows live only while a unit is in flight). jurisdiction_fund holds policy
-- (floor) + current pause state.

-- Money is tracked as NUMERIC(14,6): token costs are small fractions of a cent,
-- so 6 decimal places preserve per-call precision; 14 total digits leaves ample
-- headroom for aggregate balances.

CREATE TABLE IF NOT EXISTS jurisdiction_fund (
    jurisdiction_id    INTEGER PRIMARY KEY REFERENCES jurisdiction (id),
    -- Processing pauses for this jurisdiction when a unit's reservation would
    -- drop available balance below this floor. A non-zero floor is a safety
    -- buffer (covers in-flight settlement drift / estimate error).
    min_balance_floor  NUMERIC(14,6) NOT NULL DEFAULT 0,
    -- 'active'    — eligible for processing
    -- 'paused'    — auto-paused (insufficient funds); resumes when topped up
    -- 'suspended' — manually held regardless of balance (admin)
    status             TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'paused', 'suspended')),
    paused_reason      TEXT,
    paused_at          TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Append-only. Every permanent money movement is one row; balance is the SUM.
-- Never UPDATE/DELETE these rows — corrections are new 'adjustment' rows so the
-- history stays a complete audit trail.
CREATE TABLE IF NOT EXISTS fund_ledger (
    id               BIGSERIAL PRIMARY KEY,
    jurisdiction_id  INTEGER NOT NULL REFERENCES jurisdiction (id),
    -- 'deposit'    — funds added (manual allocation, grant, donation) [+]
    -- 'spend'      — settled processing cost from real usage              [−]
    -- 'refund'     — funds returned                                       [+]
    -- 'adjustment' — manual correction (signed)                          [±]
    kind             TEXT NOT NULL
                     CHECK (kind IN ('deposit', 'spend', 'refund', 'adjustment')),
    -- Signed: deposits/refunds positive, spend negative. The SUM is the balance.
    amount_usd       NUMERIC(14,6) NOT NULL,
    -- What this entry is tied to, for reconciliation: e.g. ref_kind='meeting'
    -- ref_id=<meeting_id>, or 'run'/<run_id>, or 'donation'/<external id>.
    ref_kind         TEXT,
    ref_id           TEXT,
    description      TEXT,
    -- Cost breakdown for spend rows: {model: {input_tokens, output_tokens,
    -- cost_usd}, mistral_pages, ...} — keeps the audit trail self-explaining.
    meta             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS fund_ledger_jurisdiction_idx
    ON fund_ledger (jurisdiction_id);
CREATE INDEX IF NOT EXISTS fund_ledger_ref_idx
    ON fund_ledger (ref_kind, ref_id);

-- Transient holds for in-flight units. Created at reserve, deleted at settle or
-- release. amount_usd here is the ESTIMATE (expected cost); the real cost lands
-- in fund_ledger at settlement. A leftover row = a unit that died without
-- settling; a janitor can release reservations older than a TTL.
CREATE TABLE IF NOT EXISTS fund_reservation (
    id               BIGSERIAL PRIMARY KEY,
    jurisdiction_id  INTEGER NOT NULL REFERENCES jurisdiction (id),
    run_id           UUID,
    meeting_id       INTEGER REFERENCES meeting (id),
    job_name         TEXT,
    amount_usd       NUMERIC(14,6) NOT NULL,   -- estimated hold
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS fund_reservation_jurisdiction_idx
    ON fund_reservation (jurisdiction_id);
CREATE INDEX IF NOT EXISTS fund_reservation_meeting_idx
    ON fund_reservation (meeting_id);
