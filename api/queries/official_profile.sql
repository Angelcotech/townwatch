-- TownWatch — Official Profile Query
-- Returns everything needed to render a full official profile page.
-- This is the primary performance benchmark for the schema.
-- Replace :official_id with the target official's ID.

-- 1. Core identity + current seat
SELECT
    o.id,
    o.canonical_name,
    o.first_name,
    o.last_name,
    o.party_affiliation,
    o.photo_url,
    o.bio_text,
    o.official_website,
    s.name              AS seat_name,
    s.seat_type,
    s.district_name,
    gb.name             AS body_name,
    gb.body_type,
    j.display_name      AS jurisdiction_name,
    j.state_abbr,
    t.start_date        AS term_start,
    t.end_date          AS term_end,
    t.how_seated,
    t.party_at_time,
    t.vote_share
FROM official o
JOIN term t         ON t.official_id = o.id AND t.is_current = true
JOIN seat s         ON s.id = t.seat_id
JOIN governing_body gb ON gb.id = s.governing_body_id
JOIN jurisdiction j    ON j.id = gb.jurisdiction_id
WHERE o.id = :official_id;


-- 2. Full career history (all terms)
SELECT
    t.start_date,
    t.end_date,
    t.how_seated,
    t.party_at_time,
    t.election_cycle_year,
    t.vote_share,
    s.name          AS seat_name,
    gb.name         AS body_name,
    j.display_name  AS jurisdiction_name
FROM term t
JOIN seat s         ON s.id = t.seat_id
JOIN governing_body gb ON gb.id = s.governing_body_id
JOIN jurisdiction j    ON j.id = gb.jurisdiction_id
WHERE t.official_id = :official_id
ORDER BY t.start_date DESC;


-- 3. Voting record (most recent 100 votes, with motion context)
SELECT
    m.meeting_date,
    mt.title,
    mt.motion_type,
    mt.outcome,
    mt.vote_tally_yes,
    mt.vote_tally_no,
    v.vote_value,
    v.notes         AS vote_notes,
    mt.full_text_url,
    ds.record_url   AS source_url,
    ds.ingested_at
FROM vote v
JOIN motion mt  ON mt.id = v.motion_id
JOIN meeting m  ON m.id = mt.meeting_id
JOIN data_source ds ON ds.id = v.data_source_id
WHERE v.official_id = :official_id
ORDER BY m.meeting_date DESC
LIMIT 100;


-- 4. Campaign contributions by election cycle
SELECT
    election_cycle_year,
    contributor_type,
    COUNT(*)                AS contribution_count,
    SUM(amount)             AS total_amount,
    MAX(amount)             AS largest_single
FROM campaign_contribution
WHERE official_id = :official_id
GROUP BY election_cycle_year, contributor_type
ORDER BY election_cycle_year DESC, total_amount DESC;


-- 5. Top donors (all time)
SELECT
    contributor_name,
    contributor_type,
    contributor_employer,
    contributor_state,
    COUNT(*)        AS contribution_count,
    SUM(amount)     AS total_amount,
    MIN(contribution_date) AS first_contribution,
    MAX(contribution_date) AS last_contribution
FROM campaign_contribution
WHERE official_id = :official_id
GROUP BY contributor_name, contributor_type, contributor_employer, contributor_state
ORDER BY total_amount DESC
LIMIT 50;


-- 6. Property holdings over time
SELECT
    parcel_id,
    situs_address,
    situs_city,
    situs_state,
    property_type,
    assessment_year,
    assessed_value_total,
    market_value,
    exemptions,
    owner_name_raw,
    ownership_type,
    deed_recorded_date,
    ds.record_url   AS source_url,
    ds.ingested_at
FROM property_record pr
JOIN data_source ds ON ds.id = pr.data_source_id
WHERE pr.official_id = :official_id
ORDER BY parcel_id, assessment_year DESC;


-- 7. Total assessed wealth by year (chart data)
SELECT
    assessment_year,
    SUM(assessed_value_total)   AS total_assessed,
    SUM(market_value)           AS total_market,
    COUNT(DISTINCT parcel_id)   AS parcel_count
FROM property_record
WHERE official_id = :official_id
GROUP BY assessment_year
ORDER BY assessment_year ASC;
