# Georgia Jurisdiction Recon Methodology

**Purpose.** Build a durable, structured registry of Georgia jurisdictions so TownWatch knows
what an onboarding will require *before* it starts. Each entry captures the components that
determine onboarding cost and feasibility: the civic publishing platform, whether a records
custodian email is publicly reachable, the records-request intake method, the official site,
and the agenda/minutes source.

This is a recon registry, **not** a set of jurisdiction configs. It feeds onboarding triage.
It does NOT modify or replace `jurisdictions/{slug}.json` — a registry entry graduates into a
full config only when a jurisdiction is actually onboarded.

---

## 1. The universe of Georgia jurisdictions

| Layer | Count | Source |
|---|---|---|
| Counties | **159** | U.S. Census Bureau Census of Governments; Carl Vinson Institute of Government (UGA). Georgia has more counties than any state except Texas. |
| Municipalities (cities/towns) | **~535** | Georgia Municipal Association (GMA) / GA Dept. of Community Affairs (DCA) registered active municipalities; Census 2022 Census of Governments lists 535 incorporated places/sub-county general-purpose governments. |
| School systems | **~180** (181 county + city systems; ~182 unified per Census) | GA Department of Education; Census Census of Governments (182 unified school districts + 3 dependent). 159 county systems + ~21 independent city systems. |

Consolidated governments (e.g., Augusta–Richmond County, Athens–Clarke County, Columbus–Muscogee,
Macon–Bibb) collapse a county and its principal city into a single general-purpose government —
recon them once, as a consolidated entity, not twice.

**Authoritative source lists to drive coverage:**
- Counties + seats: ACCG (Association County Commissioners of Georgia); Census; Wikipedia "List of counties in Georgia" (cross-checked).
- Municipalities: GMA member directory; DCA "Local Government Directory"; Census "List of municipalities in Georgia."
- School systems: Georgia Dept. of Education (gadoe.org) system directory; NCES (LEAID).
- Population: Census Vintage 2024 county estimates (used for the "top 10 by population" batch).

---

## 2. Recon dimensions (the fields that drive onboarding cost)

For every jurisdiction we classify the following.

### 2.1 `agenda_platform` — how agendas/minutes are PUBLISHED
The single biggest driver of scraper reuse. Controlled vocabulary:

| Value | What it is | Onboarding cost signal |
|---|---|---|
| `civicplus_civicengage` | CivicPlus "AgendaCenter" (`/AgendaCenter`, `ViewFile/Agenda/...`) | **Low** — stable URL pattern, existing scraper (Grovetown). |
| `civicclerk` | CivicClerk portal (`*.portal.civicclerk.com` + OData `*.api.civicclerk.com/v1/`) | **Low** — JSON API, existing client (Columbia County). |
| `granicus_legistar` | Granicus / Legistar (`*.legistar.com`, `*.granicus.com`) | **Medium** — InSite/Legistar API exists but per-tenant config; common in large counties. |
| `boarddocs` | Diligent BoardDocs (`go.boarddocs.com/ga/...`) | **Medium** — dominant for school boards; JS-rendered, predictable AJAX endpoints. |
| `edlio` | Edlio school CMS (`apps/pages/index.jsp?uREC_ID=...`) | **High** — docs often Google-Docs-backed, agendas frequently missing (CCSD). |
| `simbli_eboard` | eBOARDsolutions Simbli (`simbli.eboardsolutions.com`) — a GSBA product | **Medium** — GA school-board native; structured but per-district. |
| `diligent_community` | Diligent Community (formerly iCompass) | **Medium**. |
| `municode_meetings` | Municode Meetings / CivicSend | **Medium**. |
| `granicus_govqa` | (intake only — see 2.3) | n/a for agendas |
| `custom` | Home-grown HTML page with linked PDFs | **High** — bespoke scrape, brittle. |
| `wordpress_pdf` | WordPress/generic CMS with manually-posted PDFs | **High**. |
| `none_found` | No agendas/minutes published online | **Highest** — open-records-only; audit FINDING (OCGA 50-14-1(e)). |
| `unknown` | Not yet checked | — |

### 2.2 `clerk_email_access` — is a records custodian / clerk EMAIL publicly reachable?
| Value | Meaning |
|---|---|
| `plaintext` | A real custodian/clerk email is printed in plaintext on the official site (e.g. Columbia County `pcrawley@…`). |
| `role_alias` | Only a role alias is reachable (e.g. `clerk@city…`), not a named person, but it IS emailable. |
| `obfuscated` | Emails exist but are image/JS-obfuscated or behind "staff directory" contact forms. |
| `portal_only` | No email; requests go only through a portal/web form (CCSD → NextRequest). |
| `none_found` | No custodian contact of any kind located. |
| `unknown` | Not checked. |

This matters because TownWatch's live-forum / open-records auto-send needs an email recipient;
`portal_only` and `obfuscated` jurisdictions require form-submission support, not email.

### 2.3 `records_intake` — how an open-records request is SUBMITTED
| Value | Meaning |
|---|---|
| `email` | Email to a custodian is the published method. |
| `nextrequest` | NextRequest portal (`*.nextrequest.com`). |
| `govqa` | GovQA / Granicus GovQA portal. |
| `justfoia` | JustFOIA portal. |
| `web_form` | Generic web form on the official site (not a named ORR platform). |
| `mail_or_fax` | Only postal mail / fax accepted. |
| `phone` | Phone-only. |
| `unknown` | Not checked. |

### 2.4 Identity / supporting fields
`official_website`, `agenda_source_url`, `records_custodian_name`, `records_custodian_title`,
`records_custodian_email`, `notable_gaps[]`.

### 2.5 `verified` + `confidence` — HONESTY fields
- `verified`: `"verified"` = a human/agent actually loaded the official site (or its portal) and
  read the value off the page during this recon. `"inferred"` = derived from secondary sources,
  platform fingerprints, or pattern-matching without confirming on the live site.
- `confidence`: `high` / `medium` / `low`. A `verified` entry is normally `high`. An `inferred`
  entry is `medium` at best; `low` flags a guess that needs a site visit before use.

**Rule: a verified entry beats ten guesses.** Never mark `verified` for a value you did not see
on the live site. When unsure, mark `inferred` + `low` and add a note.

---

## 3. How to classify (procedure per jurisdiction)

1. Find the official site (`.gov`, `cityof*.com`, `*countyga.gov`, school `*.k12.ga.us` / vendor domain).
2. Locate the agendas/minutes section. Fingerprint the platform from the URL:
   - `/AgendaCenter` → CivicPlus; `portal.civicclerk.com` → CivicClerk; `legistar`/`granicus` → Granicus;
     `go.boarddocs.com` → BoardDocs; `simbli.eboardsolutions.com` → Simbli; `apps/pages/index.jsp` → Edlio.
3. Find the open-records / records-custodian page. Record the intake method and whether an email is plaintext.
4. Note gaps: empty agenda pages, dead document links, stub PDFs, portal-only intake, no online minutes.
5. Set `verified`/`confidence` honestly based on whether you saw it live.

## 4. Onboarding cost model (derived)

`onboarding_cost ≈ f(agenda_platform reuse, intake automatability, gap remediation)`

- **Cheapest:** known platform with existing scraper (`civicplus_civicengage`, `civicclerk`) **plus**
  `email` or `role_alias` intake → fully automatable, no new code.
- **Mid:** `granicus_legistar` / `boarddocs` / `simbli_eboard` (one-time per-vendor client work, then reusable)
  with `nextrequest`/`govqa` portal intake (needs portal-submission support, but standardized).
- **Hardest:** `custom` / `edlio` / `none_found` agendas with `obfuscated` / `portal_only` / `mail_or_fax`
  intake → bespoke scraping AND non-email delivery; often an audit finding rather than a clean ingest.
