-- Migration 008: official.display_title
--
-- The displayed title on the city's bio page ("Mayor Pro Tem",
-- "Councilmember", etc.) often differs from the formal seat name in our
-- config ("Council Member 1"). We store both so the UI can show whichever
-- is more useful in context.

ALTER TABLE official
    ADD COLUMN display_title TEXT;
