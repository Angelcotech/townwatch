# GA Jurisdiction Universe — Source Verification & Roster Provenance

**Date:** 2026-06-10
**Companion artifacts:** `universe_roster.json` (the enumerated universe + recon-coverage diff) and `registry.json` (per-jurisdiction recon detail). This document merges the two research efforts of 2026-06-10: (a) the primary-data roster build and (b) an adversarially-verified deep-research review of authoritative sources (103 agents; 20 sources fetched; 95 claims extracted; 25 verified by 3-vote adversarial panels → 24 confirmed, 1 refuted).

## 1. The verified universe (June 2026)

| Layer | Count | Basis |
|---|---|---|
| Counties | **159** | Canonical FIPS set 13001–13321 (skipping unassigned 13041, 13203). DCA convention: 151 county govts + 8 consolidated = 159. Census of Governments 2022: 152 county-typed + 7 municipal-typed consolidations = 159. Both reconcile exactly. |
| Municipalities | **536** | GMA member directory = 536; TIGERweb ACS25 (boundaries Jan 1 2025) = 536 FUNCSTAT=A places; Census sub-est2024 = 535 (predates Mulberry). CoG 2022 = 537 (types 7 consolidations as municipal). |
| Public school systems | **180** | NCES CCD 2024-25, agency_type=1: 159 county + 21 independent city systems. Raw CCD GA count (253) includes 52 charter-specialty LEAs, 16 RESAs, 4 state-agency LEAs, 1 State Schools LEA — excluded. |
| **Total** | **875** | Roster covers all three layers; recon registry covers 66 → **809 remaining**. |

## 2. Verified findings (each survived 3-vote adversarial verification)

1. **Municipality count 536–537 (3-0).** GMA directory live-displays 536; TIGERweb ACS25 has 538 place records of which 536 are active (2 are fictitious Athens/Augusta "balance" records); CoG 2022 = 537 municipal units. The deltas are entirely consolidation/balance-record accounting.
2. **All five post-2015 incorporations active, zero dissolutions (3-0).** South Fulton (1372122), Stonecrest (1373784), Tucker (1377652), Peachtree Corners (1359735), Mableton (1348288, inc. 2023) — all FUNCSTAT=A. Mableton de-annexation bills failed in the General Assembly.
3. **Eight consolidated governments, not four (3-0/3-0/2-1).** DCA "All Active Georgia Governments by Type" lists: Athens-Clarke, Augusta-Richmond, Columbus-Muscogee, Macon-Bibb, Cusseta-Chattahoochee, Georgetown-Quitman, Echols, Webster. CoG 2022 types 7 as municipal (Echols stays county-typed). CoG populations: Columbus 205,617; Augusta-Richmond 201,196; Macon-Bibb 156,762; Athens-Clarke 128,711; Cusseta-Chattahoochee 9,048; Webster 2,367; Georgetown-Quitman 2,243.
4. **Census normalization trap (3-0).** In TIGERweb's incorporated-places layer, Athens-Clarke and Augusta-Richmond exist ONLY as FUNCSTAT=F "(balance)" records, while Macon-Bibb and Columbus are normal active places; the separate `concity` layer holds only Athens-Clarke and Augusta-Richmond. Raw row counts from either layer are wrong without explicit normalization.
5. **180 regular LEAs = 159 county + 21 city (3-0).** Verified across all 17 NCES CCD locator pages AND the CCD directory file via Urban Institute API. Historical city/county school mergers are baked into county-system names (Savannah-Chatham, Griffin-Spalding, Thomaston-Upson).
6. **The 21 independent city school systems (3-0):** Atlanta Public Schools, Bremen City, Buford City, Calhoun City, Carrollton City, Cartersville City, Chickamauga City, City Schools of Decatur, Commerce City, Dalton Public Schools, Dublin City, Gainesville City, Jefferson City, Marietta City, Pelham City, Rome City, Social Circle City, Thomasville City, Trion City, Valdosta City, Vidalia City. No mergers/dissolutions in 2024-25 data.

## 3. Canonical sources per layer (verified live 2026-06-10)

### Counties
- Canonical: the 159-FIPS set; population from Census PEP `co-est2024-alldata.csv` (https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/counties/totals/co-est2024-alldata.csv).
- Cross-check: DCA "All Active Georgia Governments by Type" (https://dca.georgia.gov/document/publications/all-active-georgia-governments-type/download) — authoritative for the 151+8 decomposition and consolidation FIPS conventions, but **stale (Feb 14, 2022)**.

### Municipalities
- Canonical membership roster: **GMA member-cities directory** (https://www.gacities.com/gma-cities-districts/gma-member-cities). No CSV/API, but the full 536-city dataset (names, addresses, phones) is embedded in the initial HTML of a single unauthenticated GET — trivially scrapeable; "Load More" is client-side only (verified 3-0).
- Current legal-boundary cross-check: **TIGERweb ACS25** GA incorporated places (https://tigerweb.geo.census.gov/tigerwebmain/Files/acs25/tigerweb_acs25_incplace_ga.html) — freshest vintage (Jan 1, 2025).
- Population: Census PEP `sub-est2024.csv` (https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/cities/totals/sub-est2024.csv); SUMLEV 162 = place, SUMLEV 157 = place-by-county parts (used for county mapping).
- Bulk government-universe file: **Census of Governments 2022 Government Units** (https://www2.census.gov/programs-surveys/gus/datasets/2022/govt_units_2022.ZIP, ~11 MB xlsx; GA = 689 general-purpose rows). Jan 2022 snapshot — **predates Mableton and Mulberry**; no newer vintage exists (2-1 + 3-0).
- DCA FIPS crosswalk PDFs: "Municipalities Alpha with County and FIPS Codes" (https://dca.georgia.gov/document/publications/alphabetical-listing-all-cities-fips-and-resident-county-fips/download) — 550 entries incl. dissolved Bibb City, omits Mableton; both DCA PDFs frozen at Feb 14, 2022 (3-0).
- DCA Local Government Contact Database: covers all three general-purpose layers but **credential-gated** (CICOID + password); no public read path. Export may be requestable via dca.research@dca.ga.gov (3-0).

### School systems
- Canonical machine-readable: **NCES CCD LEA Universe Survey** flat files — current releases at https://nces.ed.gov/ccd/files.asp (note: nces.ed.gov/ccd/pubagency.asp is a legacy page ending at SY 2018-19). Convenient API mirror: Urban Institute Education Data API (`/school-districts/ccd/directory/{year}/?fips=13`, filter `agency_type=1`). CCD lags 1–2 school years.
- Fresher but unverified machine-readability: GaDOE's own directory (open question below).

## 4. How the two research sets reconcile (roster provenance)

- `universe_roster.json` was built from the primary datasets above (PEP counties/places + CCD LEAs), then diffed against `registry.json` coverage (21/159 counties, 11/536 municipalities, 34/180 school systems covered).
- The deep-research review then surfaced one census-vintage gap, confirmed by direct TIGERweb diff: **Mulberry** (Gwinnett, GEOID 1353706, incorporated 2024) — added to the roster manually with `pop_2024: null` (no PEP estimate until Vintage 2025). Roster municipality count now matches GMA/TIGERweb at 536.
- The roster's independent build agreed with every other verified count before reconciliation: 159 counties, 180 LEAs (159+21), and the 6 consolidated-government place records (Athens-Clarke/Augusta-Richmond correctly excluded as F-status balance records).
- Population rankings in the roster come from PEP Vintage 2024 (the deep-research run flagged that no verified claim covered rankings; the roster supplies them directly from the primary files).

## 5. Refuted / excluded claims

- "TIGERweb GA breakdown is 429 LSADC=25 cities + 102 LSADC=43 towns, with Echols coded CG" — **refuted 1-2**. Do not rely on LSADC city/town splits without re-verification.

## 6. Open questions / watch items

1. Post-Jan-2025 incorporations or dissolutions (anything newer than the TIGERweb ACS25 vintage) — re-diff GMA vs TIGERweb on each roster refresh.
2. Does GaDOE publish a current machine-readable LEA directory (CSV/API) fresher than CCD? Would become the canonical school-system source if so.
3. Will DCA provide a current export of its contact database (dca.research@dca.ga.gov)? Would supersede the stale Feb-2022 PDFs and add custodian-adjacent contact data to recon.
4. Minor precision: the "52 State Specialty Schools" charter-LEA count and Echols's county-vs-municipal typing differ slightly across sources; immaterial to the 159/536/180 layer counts.

## 7. Stats (deep-research run wf_76553993-a93)

5 search angles → 20 sources fetched (3 URL dupes filtered, 6 dropped on budget) → 95 claims extracted → 25 top claims through 3-vote adversarial verification → 24 confirmed, 1 refuted → 12 synthesized findings. 103 agents, ~2.16M subagent tokens, 794 tool uses, 57 min wall clock. One fetch agent died on a certificate-verification error (its source was not load-bearing).

---

# Verification pass — 2026-06-10 (second sweep over all flagged findings)

Independent re-verification of every flagged item (4 parallel agents, live fetches, adversarial framing: each agent instructed to try to DISPROVE the claim). Verdicts: **16 confirmed, 4 corrected, 2 upgraded**, plus one open question answered and one new fact (statutory dissolutions).

## Corrections applied to registry.json (changelog 0.2.1)

1. **Gwinnett County Public Schools** — both component facts held (7 PM Wednesday sign-up deadline; Monday-noon agenda posting) but the *inference* was wrong: business meetings are the **third Thursday**, so the Wednesday deadline is the day before the meeting — ~2 days **after** agenda publication, not before. Gap note rewritten.
2. **Glascock County Consolidated Schools** — "no staff emails" sub-claim refuted: department staff emails ARE plaintext on staff pages (HR, Finance, etc.). Board/superintendent emails and any custodian designation remain unpublished. The core findings (no agendas/minutes online; participation link 404) re-confirmed live.
3. **Columbia County School District** — the Board-Meetings page that iframed the public-participation form (uREC_ID=4394898) now returns **404**; the Edlio form itself still works (reCAPTCHA still active). `submit_url` repointed to the direct form URL; the broken parent page recorded as a navigation finding.
4. **Paulding County School District** — minutes have caught up (48/50 published; only 05/19/2026 and the future meeting unpublished). Lag note softened.

## Upgrades (stronger than originally flagged)

- **Houston County Schools** — not a "lag": **all 50 meetings** on the Simbli listing (May 2023 → June 2026) show minutes UNPUBLISHED. The district appears to never publish minutes via Simbli — standing OCGA 50-14-1(e)(2) finding.
- **Mulberry** — Census **Vintage 2025** sub-county estimates are now published (`sub-est2025_13.csv`) and include Mulberry: POPESTIMATE2025 = 41,445 (2024 back-cast 41,170). Roster updated; Mulberry ranks ~#23 among GA municipalities.

## Confirmed unchanged (spot list)

Taliaferro absence findings; Bibb `none_found` comment channel; Hancock minutes lag (13 May–June 2026 meetings); the five rural CSRA no-ORR districts (McDuffie, Jefferson, Lincoln, Wilkes¹, Warren); Cobb ID-required in-person comment; Cherokee in-person + security screening; DeKalb deadlines and form-only ORR; Fulton email-refusing portal-only ORR; all 5 legacy BoardDocs instances still resolve; Richmond NextRequest still answers HTTP 204 to plain clients; all four off-domain form artifacts still live; all five wrong-state lookalike domains re-confirmed; DCA contact DB still credential-gated; 8 consolidated governments re-confirmed from the DCA PDF; no post-Jan-2025 incorporations or dissolutions found.

¹ Wilkes now serves a JS anti-bot "Client Challenge" to plain HTTP clients — new automation hazard recorded.

## Open question answered: GaDOE machine-readable directory

**No.** `data.gadoe.org` does not exist (NXDOMAIN); GaDOE's public data hub is Georgia Insights, whose "Data Downloads" page is a Power BI iframe (no direct CSV/Excel/API); the legacy contacts export (`archives.gadoe.org/findaschool.aspx?contacts=ALL`) now redirects to the homepage (retired). Practical alternatives: GOSA's download repository (district-level CSVs, also JS-listed) or NCES CCD files. **NCES CCD remains the canonical machine-readable LEA source.**

## New fact: statutory municipal dissolutions (pre-2025)

Georgia dissolves non-functioning municipalities by statute. Two were repealed between the 2023 and 2025 gazetteer vintages — **Ranger** (Gordon Co., HB 773, May 2023; town had not held an election since 2005) and **Sunny Side** (Spalding Co., HB 542, effective 2024-01-01). Both were stale rows in the seeded `jurisdiction_directory` (2023 vintage) and are now removed; the seed job gained a stale-row cleanup step (uncovered rows only; covered rows warn for human review).

## Implementation note

The searchable `jurisdiction_directory` (powering the public "find your town" search) was re-seeded to match this verified universe: Census **2025** gazetteer (place/county/unsd; pipe-delimited — format changed from tab in 2023), school-district layer added (migration 055; DoDEA Fort Stewart excluded by GEOID — name-based exclusion is unsafe because civilian "Fort X" districts exist in other states), Mulberry present, Ranger/Sunny Side removed. GA directory now: **538 city rows (536 municipalities + 2 consolidated balance entries) + 159 counties + 180 school districts = 877 searchable**, 3 covered.
