# South Carolina — Universe Verification (Tier-0 seeding + Phase B)

**Status:** Tier-0 seeded + Phase B universe verified, 2026-06-13.
**Canonical roster:** the seeded `jurisdiction_directory` (DB), snapshot exported to
`universe_roster.json` (same dir). Reproduce from source with:

```
PYTHONPATH=etl etl/.venv/bin/python -m townwatch_etl.jobs.seed_jurisdiction_directory --state SC
PYTHONPATH=etl etl/.venv/bin/python -m townwatch_etl.jobs.backfill_directory_nav --state SC
```

Seed source: Census **2025 gazetteer** (place / county / unified-school-district,
legal boundaries as of Jan 1 2025). Place→county nav: Census **SUB-EST 2024**
SUMLEV-157 place-parts (national; replaced the GA-only universe roster — see Errata).

---

## Verified counts (every layer reconciles across ≥2 independent authorities)

| Layer | Seeded | Authority A | Authority B | Delta | Verdict |
|---|---|---|---|---|---|
| Counties | **46** | SC Association of Counties (46) | Census 2022 CoG (46) | 0 | ✅ UPHELD (3/3) |
| Municipalities | **271** | Municipal Association of SC — "271 incorporated cities and towns" | Census 2022 CoG (271 municipal) | 0 | ✅ UPHELD (3/3) |
| School districts | **72** | SC School Boards Association — "72 public school districts" | NCES/CCD + SC DOE rosters (72) | 0 | ✅ UPHELD (3/3) |

Adversarial method: 3 independent agents instructed to **refute** each count/claim
(2026-06-13). All layers survived 3/3. No unexplained delta in any layer — the
rare case where the municipal-league count and the Census incorporated-place count
coincide exactly.

## Findings (vote tallies = agents upholding / total)

- **F1 — Municipalities = 271 (3/3).** MASC's live figure (271) equals the Census
  FUNCSTAT-A incorporated-place count (271). No 2024–2025 incorporation or
  dissolution found that would move it; SC incorporation is statutorily hard
  (population/density thresholds, 5-mile rule). Canonical source: **MASC**.
- **F2 — Counties = 46 (3/3).** Fixed since 1919 (Allendale, last county formed);
  "maximum allowable by state law." Canonical source: **SC Association of Counties**.
- **F3 — School districts = 72 (3/3).** SC uses **numbered sub-county districts**
  (multiple independent districts per county: Anderson 1–5, Spartanburg 1–7,
  Lexington 1–5, York 1–4, Greenwood 50–52, Florence 1/2/3/5, Dillon 3/4, Laurens
  55/56, Richland 1/2, Dorchester 2/4, Marion 10, plus Sumter County Consolidated).
  This is why 72 ≫ 46 counties. Canonical source: **SC DOE / SCSBA**.
- **F4 — No city-county consolidations (3/3).** SC's constitution (Art. VIII)
  authorizes consolidation by referendum but it has never been exercised — no SC
  entry in any U.S. consolidated-government inventory. (Contrast GA's four.) The
  seed found **0** consolidated-balance place rows for SC, consistent with this.
- **F5 — No independent-city county-equivalents (3/3).** SC is not among the four
  independent-city states (MD/MO/NV/VA). Its 46 counties are its only
  county-equivalents; every one of the 271 municipalities sits inside a county.
- **F6 — Governance (3/3).** Counties: **County Council** under the Home Rule Act
  (S.C. Code Title 4, Ch. 9); five statutory forms (Council, Council-Supervisor,
  Council-Administrator, Council-Manager, Board of Commissioners), most common
  **Council-Administrator** (≈34/46). Municipalities: three statutory forms
  (Mayor-Council, Council, Council-Manager — S.C. Code Title 5, Ch. 5).
- **F7 — School districts are independent governments (3/3).** Per Census
  governance classification, SC K-12 districts are independent (fiscally
  independent) special-purpose governments — **no municipally-run school systems**
  (unlike GA's Decatur/Marietta city systems). NUANCE: a subset of SC districts are
  fiscally *dependent* (county council sets their levy); this is district-specific
  and is resolved per-district in Phase C, not a statewide property.

## Canonical source per layer

| Layer | Canonical authority | URL |
|---|---|---|
| Counties | SC Association of Counties | https://www.sccounties.org/county-information |
| Municipalities | Municipal Association of South Carolina | https://www.masc.sc/ |
| School districts | SC Dept. of Education / SC School Boards Association | https://ed.sc.gov/ · https://scsba.org/about-us/ |
| Cross-reference (all layers) | Census 2022 Census of Governments | https://www.census.gov/programs-surveys/economic-census/year/2022/economic-census/data/governments-data.html |
| Governance forms | S.C. Code Title 4 Ch. 9 (county) / Title 5 Ch. 5 (municipal) | https://www.scstatehouse.gov/code/t04c009.php · https://www.scstatehouse.gov/code/t05c005.php |

## Provenance

- Tier-0 seed run 2026-06-13 → 271 cities + 46 counties + 72 school districts
  (72 bundled, 0 unmatched bundle warnings after the derivation fix; 0 linked —
  no SC jurisdiction onboarded yet). Backfill → 389 rows, 0 without county_fips.
- 26 SC municipalities span >1 county (e.g. North Charleston: Charleston + Berkeley
  + Dorchester; Summerville: Dorchester + Berkeley + Charleston) and list under each
  via `nav_county_fips`. Display primary = largest-population county part.
- Roster↔registry diff (vs `registry.json`, the Tier-2 Dorchester recon): the only
  jurisdiction FIPS referenced (Dorchester County 45035) is present in the universe;
  Dorchester DD2 (4502010) + DD4 (4500002) are in the seeded district set —
  consistent. (The "45" the diff flags is the registry's state-meta FIPS, not a
  jurisdiction.)

## Errata / watch-items

- **WATCH — statutory consolidation mandate.** SC Bill 3470 (2025–26) mandates
  countywide districts by **July 1, 2027** and full consolidation by **2032**,
  forcing SC toward ~46 one-per-county districts. The current 72 reflects pre-2027
  boundaries. Re-verify the district count and bundle map against a post-2027 Census
  vintage + SC DOE roster when it lands. Tracked as the `dd2_dd4_consolidation_watch`
  quirk (statewide analog) in the ledger.
- **Tooling errata (resolved).** The place→county source was the GA-only
  `research/ga_recon/universe_roster.json`; for state #2 it was rewritten to derive
  from Census SUB-EST nationally (`backfill_directory_nav.py`). Verified 2026-06-13
  to reproduce the verified GA roster's place→county sets exactly (0 mismatches /
  535 shared places); the one roster place SUB-EST lacks (Mulberry GA, incorporated
  2023, ahead of the popest vintage) is carried as an explicit override.
- **DoDEA.** SC's UNSD gazetteer set has **no** DoDEA district to exclude — the
  Laurel Bay (MCAS Beaufort) DoDEA schools are federal and not enumerated as a
  local UNSD. (`Beaufort County School District` is a real local district; it only
  matched a "fort" substring filter.) `DODEA_UNSD_GEOIDS` needs no SC addition.
