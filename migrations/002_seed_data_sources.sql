-- TownWatch — Seed Data Sources
-- Migration 002: Known source systems with provenance metadata

INSERT INTO data_source (source_name, source_type, source_url, notes) VALUES
    ('CensusTIGER',     'bulk_download', 'https://www.census.gov/geographies/mapping-files/time-series/geo/tiger-line-file.html', 'Census TIGER/Line shapefiles — jurisdiction boundaries and FIPS codes'),
    ('BallotReady',     'api',           'https://organizations.ballotready.org/officeholders-api',                               'BallotReady/CivicEngine officeholder API — current and historical local officials'),
    ('CTCL',            'bulk_download', 'https://www.techandciviclife.org/our-work/research-department/governance-project/',     'Center for Tech and Civic Life Governance Project — county and major city officials'),
    ('FollowTheMoney',  'api',           'https://www.followthemoney.org/our-data/data-downloads/',                               'National Institute on Money in Politics — campaign contributions, all 50 states'),
    ('CountyAssessor',  'bulk_download', NULL,                                                                                    'County assessor parcel data — URL set per jurisdiction in jurisdiction config'),
    ('MuniClerk',       'scrape',        NULL,                                                                                    'Municipal/county clerk website — meeting agendas, minutes, voting records. URL set per jurisdiction');
