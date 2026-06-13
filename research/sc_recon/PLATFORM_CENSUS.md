# South Carolina — Platform Census (Phase D)

**Status:** Phase D complete, 2026-06-13. Builds on the verified universe (Phase B,
`UNIVERSE_SOURCES.md`) and bundle map (Phase C, `STRUCTURE.md`). Stratified sample of
**46 jurisdictions** fingerprinted in `registry.json`: all 10 top-population counties +
their seats + the 3 largest non-seat cities + the largest school districts (sampling the
numbered-district fanout) + a 4-county rural control (county + district each). Dorchester
County's bundle (county + DD2 + DD4) came from the earlier Tier-2 recon and is folded in.

Method: per-jurisdiction live fingerprint + search-engine pass + sibling control, controlled
vocabularies reused verbatim from `research/ga_recon/METHODOLOGY.md`. Census confidence caps
at `medium` for any entry carrying an unrefuted absence (no per-entry adversarial pass — that
is Tier-2 onboarding depth). Honesty fields (`verified`/`inferred`, `confidence`) are per entry.

---

## Platform distribution (verified live)

### Counties (n=15) — the large ones are BESPOKE
| Platform | Count | Who |
|---|---|---|
| `custom` (in-house / GovAccess / Revize, no agenda product) | **7** | Greenville (.aspx), Charleston (CCMIMS), Richland (GovAccess), Horry (Revize), + rural Allendale/McCormick/Lee |
| `granicus_legistar` | 2 | Lexington, **Dorchester** |
| `civicclerk` | 2 | York, Beaufort |
| `wordpress_pdf` | 2 | Anderson, Bamberg |
| `civicplus_civicengage` | 1 | Spartanburg |
| `granicus_iqm2` | 1 | Berkeley |

**Finding C-1:** the four largest counties (Greenville, Charleston, Richland, Horry) **all run
custom, self-hosted agenda systems** — no off-the-shelf platform. The off-the-shelf vendors
(CivicClerk, Granicus, CivicPlus) appear only in the rank 5–10 tier. Rural counties are
custom too (Revize CMS document-centers), so `custom` is bimodal: biggest + smallest.

### Municipalities (n=13) — CivicPlus plurality
| Platform | Count | Who |
|---|---|---|
| `civicplus_civicengage` (AgendaCenter) | **5** | Charleston, Lexington (town), Beaufort, Mount Pleasant, Rock Hill |
| `wordpress_pdf` (posted PDFs, no AgendaCenter) | 4 | Conway, Spartanburg, York, Anderson (city) |
| `civicclerk` | 1 | Greenville (mid-migration off CivicEngage) |
| `granicus_iqm2` | 1 | Columbia (capital) |
| `municode_meetings` | 1 | Moncks Corner |
| `custom` | 1 | North Charleston (Revize/.php + Google Drive) |

### School districts (n=18) — BoardDocs dominant; **Diligent owns 72%**
| Platform | Count | Who |
|---|---|---|
| `boarddocs` | **10** | Greenville, Charleston, Horry, Berkeley, Richland 1, Richland 2, Spartanburg 7, Allendale, McCormick, + **Dorchester DD2** |
| `diligent_community` (iCompass / Diligent One Platform) | 3 | Lexington 1, Beaufort, + **Dorchester DD4** |
| `edlio` | 3 | Spartanburg 6, Anderson 5, Lee |
| `simbli_eboard` | 1 | York 3 (Rock Hill) |
| `finalsite` | 1 | Bamberg |

**Finding D-1 (the scraper-investment prize):** **BoardDocs is the dominant SC school-board
platform (10/18 ≈ 56%).** And because BoardDocs *and* Diligent Community are both **Diligent**
products, **Diligent owns the board-agenda layer for 13/18 = 72%** of sampled districts. This
is South Carolina's analog of Georgia's "Simbli = 27/33" finding — but a **different vendor**.
Simbli (eBOARDsolutions), Georgia's leader, appears only **once** in SC (Rock Hill).

**Finding D-2 (sibling control — do NOT infer from county):** numbered districts in the *same*
county do **not** share a platform. Spartanburg 7 = BoardDocs vs Spartanburg 6 = edlio;
York 1 = BoardDocs vs York 3 = Simbli; Anderson 1 = custom vs Anderson 5 = edlio. Each of the
72 districts must be fingerprinted independently — there is no county-level shortcut.

**Finding D-3 (migration vector):** Diligent is consolidating BoardDocs tenants onto its
"Diligent One Platform" (`*.community.diligentoneplatform.com`). Caught mid-migration this
session: Lexington 1, Beaufort County SD, and Dorchester DD4. Build for BoardDocs first, but
the Diligent Community/iCompass reader is the same-vendor follow-on, not a separate bet.

---

## Records-intake distribution (n=46)
`email` 18 · `web_form` 10 · `justfoia` 6 · `nextrequest` 4 · `mail_or_fax` 4 · `unknown` 3 · `phone` 1.
JustFOIA and NextRequest are the two SaaS FOIA portals in play (10 jurisdictions combined);
the rural tier has **no** FOIA portal at all (email/mail only) — itself an audit signal.

## Bot-wall tier (record it; needs the browser fetch layer)
Walled `.gov` HTML sites observed (HTTP 403 to raw fetch; agenda platform subdomain usually
sits OUTSIDE the wall): Richland County, Berkeley County, Rock Hill (GovAccess redesign), plus
Dorchester County (Tier-2). BoardDocs/Simbli/Diligent district portals are JS-rendered and
UA-gate the throttled client but resolve in a browser. `fetch_tier: browser` is recorded per entry.

**Liveness audit (2026-06-13, `agenda_source_url`, n=43 census entries):** 28 live (200),
12 documented walls (403 — BoardDocs `/sc/<code>` district portals + GovAccess/Finalsite
county/city sites, all JS/UA-gated and verified via browser-UA/search at fingerprint time),
3 transient (Berkeley + Columbia IQM2 `ReadTimeout`, Moncks Corner Municode
`RemoteProtocolError` — slow/protocol hiccups on live hosts, agent-verified during recon).
**Zero genuine dead/404 URLs.** Per-entry liveness was established live by the recon agents
(`verified` field); this throttled re-check corroborates with no contradiction.

---

## Scraper-investment ranking (what one client unlocks)

1. **BoardDocs** — `go.boarddocs.com/sc/<code>`. Unlocks ~56% of districts directly and is the
   single highest-ROI build for the SC school-board layer (the audit's densest body type).
   *Existing GA work:* none yet (GA went Simbli); this is the SC-specific must-build.
2. **CivicPlus CivicEngage (AgendaCenter)** — plurality municipal platform (5 cities) + Spartanburg
   County. **Existing scraper reuses directly** (Grovetown GA). Near-zero marginal cost.
3. **CivicClerk** — York County, Beaufort County, City of Greenville (+ migrations inbound from
   IQM2/CivicEngage). **Existing client reuses** (Columbia County GA OData API).
4. **Granicus (Legistar/IQM2)** — Lexington County, Dorchester County (Legistar-family);
   Berkeley County, City of Columbia (IQM2). Partial existing coverage; IQM2 is being sunset
   industry-wide toward CivicClerk, so treat IQM2 tenants as migration-watch.
5. **Diligent Community / iCompass** — DD4, Lexington 1, Beaufort SD (the BoardDocs migration
   target). Same vendor as #1; build as the BoardDocs follow-on.
6. **Custom / bespoke (the expensive long tail, but the biggest populations)** — Greenville
   County (.aspx), Charleston County (CCMIMS), Richland County (GovAccess), Horry County
   (Revize), North Charleston. Each is a one-off extractor; prioritize by population/demand.
7. **Generic PDF-directory** (`wordpress_pdf` / `edlio` / `finalsite` / Revize document-centers)
   — rural counties + small districts + several cities. One generic "linked-PDF directory"
   scraper covers the whole tail (no structured index, no API).

**Bottom line:** two builds — **BoardDocs** (schools) + the **generic PDF-directory** scraper
(rural/small) — plus reuse of the existing **CivicEngage** and **CivicClerk** clients cover the
large majority of SC jurisdictions. The expensive residual is the **four big-county custom
systems**, which are high-value (largest populations) and should be funded per-onboarding.

## What Phase D hands forward
- **To Phase E (pipeline-delta memo):** new build = BoardDocs client (highest ROI); new
  generic = PDF-directory scraper; browser fetch tier needed for ~4 walled county `.gov` sites;
  IQM2-sunset migration watch (York/Beaufort already moved to CivicClerk; Columbia still IQM2).
- **To the ledger (`states.json`, dossier session owns it):** flip `D_platform_census` → done;
  add platform quirks (handed as JSON in the session record).
- **To onboarding:** `registry.json` now carries 46 fingerprinted jurisdictions with honesty
  fields; demand-gated onboarding reads platform + intake + comment straight from each entry.
