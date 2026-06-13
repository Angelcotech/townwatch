# South Carolina — Structure & Relationships (Phase C)

**Status:** Phase C complete, 2026-06-13. Exit criteria met: zero unresolved bundle
warnings; all ledger quirks `verified` (none left `prior`). Builds on the verified
universe (Phase B — `UNIVERSE_SOURCES.md`).

The bundle map is machine-readable in the seed job's derivation
(`seed_jurisdiction_directory._derive_bundles` + `_SD_COUNTY_RE`); this file is the
human-readable record of *why* it's shaped that way.

## Bundle map (who rides whom)

| Layer | Count | Bundles to | Rule |
|---|---|---|---|
| County (County Council) | 46 | — (top of bundle) | general-purpose government; the bundle root |
| Municipality (city/town) | 271 | — (stands alone) | SC has **no** city-county consolidations, so every municipality is its own onboarding |
| School district | 72 | its **county** | `<County> School District <N>` / `<County> County Consolidated School District` → that county |

**Bundle rule:** a county's fund covers the county **+ all school districts within it**.
SC's numbered sub-county districts mean one county can carry several (Spartanburg
County rides 7; Anderson and Lexington 5 each; York and Florence 4; Greenwood 3;
Dorchester/Dillon/Laurens/Richland 2 each). **Integrity check (2026-06-13):** all 46
counties have ≥1 district (0 orphans); all 72 districts bundle to a real SC county;
0 cities carry a bundle_fips (confirming no consolidations).

District-per-county distribution: 36 counties × 1, 4 × 2, 1 × 3, 2 × 4, 2 × 5, 1 × 7.

## State-specific layering: NONE of the hard cases apply

Verified in Phase B (3/3 adversarial), so the bundle model is the simple county-root
form with no special layer:
- **No city-county consolidations** (constitution authorizes by referendum; never
  exercised). Contrast GA's four — SC needs none of that bundle machinery.
- **No independent-city county-equivalents** (SC isn't a MD/MO/NV/VA-style state).
- **No townships / parishes / boroughs** — SC's only general-purpose locals are
  counties and municipalities.
- **No independent municipal (city-run) school systems** — every district is
  county-tied (unlike GA's Decatur/Marietta city systems).

## Dependent boards (audited, trivially bundled)

Bodies without a separate government — planning commissions and boards of zoning
appeals (S.C. Code Title 6, Ch. 29, the Local Government Comprehensive Planning
Enabling Act), plus any board/commission a city or county appoints — ride their
parent general-purpose government's onboarding. No separate bundle entry: they
attach to the city or county that created them, same as GA.

## Fiscal-dependency relationship (the one audit-relevant district nuance)

SC school districts are all **independent governments** in the Census sense (separate
units), but they split on **who sets the property-tax levy** — and that decides which
body a future millage/budget finding attaches to:

- **Fiscally INDEPENDENT (~50 districts):** the board of trustees imposes the annual
  levy and certifies it to the county auditor (typically capped, e.g. ≤2 mills/yr
  increase without referendum). Finance finding → the **school board**.
- **Fiscally DEPENDENT (~22 districts):** the district cannot raise property taxes on
  its own — **County Council** sets/approves the millage. Finance finding → the
  **county council**, not the board. Dorchester School District 2 is a named example
  (corroborates the `dd2_dd4_consolidation_watch` / DD2 recon).

**Source / status:** the ~22-dependent figure is from the Post and Courier (2025–26,
Dorchester 2 budget coverage), consistent with the SC Encyclopedia "Local Government"
entry on the autonomy spectrum and per-district enabling legislation. This is a
**finance-layer-deferred** item: the exact roster of the 22 (and each one's cap) comes
from each district's enabling act (Title 59) and is enumerated at finance-ingestion
time — NOT classified per-district now, because no finance ingestion or observer
exists (same discipline as the Phase A `_sc_finance_layer_todo`). What Phase C fixes
is the **principle**: bundle geographically by county, but resolve the *levying
authority* per district when finance lands.

Sources:
- [SC Encyclopedia — Local Government](https://www.scencyclopedia.org/sce/entries/local-government/)
- [Post and Courier — Dorchester 2 budget / "22 districts" without taxing authority](https://www.postandcourier.com/education-lab/dorchester-county-district-two-school-budget-taxes/article_87c10a50-061b-4c0a-be53-c58bc5ccee65.html)
- S.C. Code Title 59 (Education) — district enabling legislation; Title 6 Ch. 29 (planning/BZA)

## What Phase C hands forward

- **To Phase D (platform census):** the stratified sample targets — top-10 counties
  by population (Greenville, Charleston, Richland, Horry, Spartanburg, Lexington,
  York, Berkeley, Anderson, Dorchester) + their seats + the largest districts, plus
  a rural sample. (Dorchester already fingerprinted by the Tier-2 recon: Granicus
  legacy + NextRequest + govAccess bot-wall — see `registry.json`.)
- **To the finance layer:** the fiscally-dependent-district roster (the 22) →
  determines the levying-authority target for millage/budget findings.
