-- Approved annual government budgets ("yearly budget automation").
-- v1 stores the TOP-LINE translation of an adopted budget: fiscal-year totals,
-- a by-fund and by-department breakdown (JSONB — light, not normalized line
-- items), and a plain-language citizen summary. Full per-account line-item
-- extraction is a later phase (would get its own budget_line_item table).
--
-- One row per (jurisdiction, fiscal_year). governing_body_id is the body that
-- adopts the budget (city_council / county_commission / board_of_education).

CREATE TABLE IF NOT EXISTS government_budget (
    id                   BIGSERIAL PRIMARY KEY,
    jurisdiction_id      INTEGER NOT NULL REFERENCES jurisdiction (id),
    governing_body_id    INTEGER REFERENCES governing_body (id),
    fiscal_year          INTEGER NOT NULL,
    -- where we got it: 'ted' (UGA Carl Vinson statewide repository), 'local'
    -- (the jurisdiction's own budget page), etc.
    source_provider      TEXT,
    source_url           TEXT,
    adopted_date         DATE,
    total_revenues       NUMERIC(16, 2),
    total_expenditures   NUMERIC(16, 2),
    fund_breakdown       JSONB,   -- [{name, revenues, expenditures}]
    department_breakdown JSONB,   -- [{name, amount}]
    plain_summary        TEXT,    -- citizen-facing "translation"
    extraction_method    TEXT,
    extraction_confidence TEXT,
    extracted_at         TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (jurisdiction_id, fiscal_year)
);

CREATE INDEX IF NOT EXISTS idx_government_budget_body
    ON government_budget (governing_body_id);
