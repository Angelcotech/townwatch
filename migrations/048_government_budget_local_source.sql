-- Budgets sourced from local meeting records (the budget-adoption meeting's
-- packet/agenda), not just the state TED filing. This is the primary, most
-- current source: a jurisdiction adopts its budget by ordinance at a meeting we
-- already scrape, even when its state filing lags years behind.
--
-- source_meeting_id: which meeting's document the budget came from (provenance).
-- meeting.budget_extracted_at: stamp so the unattended job extracts each
-- adoption meeting exactly once (idempotency), regardless of fiscal-year
-- collisions between a budget's first and second reading.

ALTER TABLE government_budget
    ADD COLUMN IF NOT EXISTS source_meeting_id INTEGER REFERENCES meeting (id);

ALTER TABLE meeting
    ADD COLUMN IF NOT EXISTS budget_extracted_at TIMESTAMPTZ;
