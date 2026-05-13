# TownWatch — Architecture

Civic accountability platform. Aggregates legally public government data and presents it as a readable timeline of who runs each town, how they vote, who funds them, and what they own.

## Stack
- **Web:** Next.js (Cloudflare Pages)
- **Backend / ETL:** Railway (cron jobs + API)
- **Database:** Postgres + PostGIS (Railway)
- **Auth:** Clerk
- **LLM:** Claude API (document extraction, plain language summaries)

## Data Sources
| Source | What | How |
|---|---|---|
| Census TIGER | Jurisdiction boundaries + FIPS | Bulk download (annual) |
| BallotReady/CivicEngine | Current + historical official rosters | GraphQL API |
| CTCL Governance Project | County + major city officials | Flat file download |
| FollowTheMoney | Campaign contributions, all 50 states | Bulk API |
| County Assessor | Property records (parcel + annual value) | Bulk download per county |
| Municipal/County Clerk | Meeting minutes, agendas, voting records | Scrape per jurisdiction |

## Database
See `migrations/` for full schema. 11 tables:

```
data_source          — provenance for every record
jurisdiction         — Census TIGER FIPS + PostGIS boundary
governing_body       — city council, planning commission, school board, etc.
seat                 — persistent position on a body
official             — canonical person record
official_alias       — name variants across sources → resolves to official
term                 — official + seat + date range (career history)
meeting              — public meeting with agenda/minutes/video URLs
motion               — agenda item voted on
vote                 — one official's vote on one motion
campaign_contribution — donation to official's campaign (FollowTheMoney)
property_record      — annual assessed value snapshot per parcel (time series)
```

## Key Design Principles
1. **Provenance first** — every row in every table points to a `data_source` record
2. **Facts only** — TownWatch presents documented public records, never editorial
3. **Time series** — property records and contributions are append-only snapshots
4. **Identity resolution** — `official_alias` maps name variants to one canonical official
5. **Config-driven** — each jurisdiction has a config file; new town = new config, not new code

## Build Phases
1. Prove the story — manual data, static viz for one town
2. Automate the pipeline — Railway cron + Claude API extraction
3. Citizen interface — Next.js web + React Native mobile
4. Reproducibility — second town validates config abstraction
5. Coordination tools — FOIA assistance, public comment mobilization
