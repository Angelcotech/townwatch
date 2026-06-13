# South Carolina — Pipeline-Delta Memo (Phase E)

**Status:** Phase E draft for founder review, 2026-06-13. This is the **human gate**:
"what breaks our automation in South Carolina," synthesized from the dossier BEFORE
any onboarding. On sign-off, flip `states.json` SC `readiness` → `ready_for_tier2`.

Inputs: Phase A (`_open_records_laws.json#SC`), Tier-0 seed + Phase B
(`UNIVERSE_SOURCES.md`), Phase C (`STRUCTURE.md`), Phase D
(`PLATFORM_CENSUS.md` / `registry.json`).

**One-line readiness read:** SC is a CLEAN expansion target — none of the hard
universe-model gaps apply (no townships/parishes/county-equivalents/consolidations),
the law layer is verified, and the platform layer needs exactly **one new must-build
(BoardDocs)** plus reuse of existing GA clients. Recommend **ready_for_tier2** after
review, with the finance layer as a known, non-blocking residual.

---

## 1. Universe-model gaps (Tier-0) — RESOLVED, SC adds none

- **Numbered sub-county school districts** (SC's one structural surprise): multiple
  independent districts per county. **Handled** — `_SD_COUNTY_RE` bundles them to
  their county (commit 9cff96a); 72/72 bundle, 0 warnings.
- **Place→county nav** generalized off the GA-only roster onto the national Census
  SUB-EST file (commit 9cff96a). **Closes the "state #2" blocker** that was sitting in
  this backlog. Residual: a place incorporated after the popest vintage needs an
  override (Mulberry GA pattern) — loud and explicit, not silent.
- SC has **none** of the model gaps that block Tier-0 elsewhere: no MCD/township
  governments, no two-tier school states, no county-equivalents (independent cities),
  no city-county consolidations. The county-root bundle model fits SC as-is.

## 2. Law-engine rules (Phase A) — verified, two cross-state traps

- **SC's audit trigger is PRE-meeting** (§ 30-4-80, 24h agenda incl. the body's
  website) — `agenda_missing` is the strongest, cleanest SC signal and our daily
  inventory cadence already watches it.
- **TRAP — do NOT port GA's minutes observer.** § 30-4-90 sets *no* numeric minutes
  deadline ("reasonable time") and *no* summary tier. GA's hard-clock
  `_MINUTES_APPROVAL_GRACE_DAYS` logic would manufacture false SC findings; an SC
  minutes observer must judge against the body's own cadence. (Already flagged in the
  `minutes_missing` `observer_note`.)
- **Campaign finance is centralized** at the SC State Ethics Commission
  (`ethicsfiling.sc.gov`) — `state_published`, never a per-jurisdiction website gap.
  One state source audits every candidate statewide.
- **Records residency:** none for *requesting* (direct-send lane, no proxy), but
  standing to *sue* is "citizen of the State" — escalation-to-litigation needs a
  SC-citizen requester of record.

## 3. Platform families (Phase D) — the build list

| Need | Platform | Coverage | Build vs reuse |
|---|---|---|---|
| **NEW must-build** | **BoardDocs** | ~56% of districts (Diligent ⇒ 72% w/ follow-on) | **New** — SC's densest-body-type unlock; GA went Simbli, so no reuse |
| NEW generic | PDF-directory scraper | rural counties + small districts + several cities (`wordpress_pdf`/`edlio`/`finalsite`/Revize) | **New** — one generic linked-PDF crawler covers the whole tail |
| Follow-on | Diligent Community / iCompass | DD4, Lexington 1, Beaufort SD (BoardDocs migration target) | New, same vendor as BoardDocs — build second |
| Reuse | CivicPlus CivicEngage (AgendaCenter) | plurality municipal (5 cities) + Spartanburg Co. | **Existing GA client** (Grovetown) — near-zero marginal cost |
| Reuse | CivicClerk | York Co., Beaufort Co., City of Greenville (+ inbound migrations) | **Existing GA client** (Columbia County OData) |
| Partial | Granicus Legistar/IQM2 | Lexington, Dorchester (Legistar); Berkeley, Columbia (IQM2) | Partial coverage; IQM2 sunsetting → migration-watch |
| Per-onboarding | Custom/bespoke | Greenville, Charleston, Richland, Horry, North Charleston | One-off extractors; high population/value → demand-fund per onboarding |

**Sibling-control rule (Finding D-2):** numbered same-county districts do NOT share a
platform — there is no county-level shortcut; each of the 72 districts is fingerprinted
independently. Bakes directly into onboarding (never infer a district's platform from
its county or a sibling).

## 4. Anti-bot posture (Phase D)

- **Browser fetch tier required** for ~4 walled county `.gov` HTML sites (Richland,
  Berkeley, Rock Hill, Dorchester — 403 to raw fetch) and for JS-rendered
  BoardDocs/Simbli/Diligent district portals. The agenda-platform subdomain usually
  sits *outside* the wall, so the document path is reachable even where the CMS isn't.
- This **extends the existing GA anti-bot ladder** item (Wilkes Apptegy challenge,
  Simbli/Diligent JS) — same recovery-ladder/headless-fallback work, more tenants. No
  portal-spoofing.
- Liveness: 0 genuine dead URLs across 43 census entries (28 live, 12 documented
  walls, 3 transient host hiccups).

## 5. Comment + records-intake lanes

- **Public comment is NOT statutory in SC** (OMA mandates none — it's each board's
  policy). So comment recon feeds the **live-forum config** (sign-up window), never a
  finding. Same record-first delivery architecture as GA; no new law-engine lane.
- **Records intake:** direct-send `email` covers 18/46; portal adapters needed for
  **JustFOIA (6)** and **NextRequest (4)** — NextRequest already on the GA backlog
  (CCSD/Augusta), so the SC delta is the **JustFOIA adapter** (new) + reusing the
  NextRequest one. Rural tier has *no* FOIA portal (email/mail only) — itself an audit
  signal, not a blocker.

## 6. Finance layer — deferred, non-blocking

The finance-transparency statutes (budget/millage/audit — SC Titles 4/5/6/12/59) are
not yet inventoried (`_open_records_laws.json#SC._sc_finance_layer_todo`), and no
finance ingestion/observer exists — so this does NOT gate Tier-2 (GA onboarded without
it). When finance lands, the Phase C fiscal-dependency split decides the levying
authority: for the **~22 fiscally dependent districts**, a millage/budget finding
attaches to **County Council**, not the board.

---

## Backlog items graduated (see `townwatch-web/BACKLOG.md`)

New "Pipeline hardening (from SC dossier, 2026-06-13)" section:
1. **BoardDocs scraper client** — highest-ROI SC build (school-board layer).
2. **Generic PDF-directory scraper** — covers the rural/small long tail.
3. **Diligent Community / iCompass reader** — BoardDocs-migration follow-on.
4. **JustFOIA portal adapter** + reuse NextRequest adapter — SC records intake.
5. **Browser fetch tier** for walled SC `.gov` sites — extends the GA anti-bot ladder.
6. **IQM2-sunset migration watch** — Columbia still IQM2; York/Beaufort already moved.
7. **Big-county custom extractors** — Greenville/Charleston/Richland/Horry, demand-funded.

Marked **done** in the backlog: "Generalize `backfill_directory_nav` off the GA roster"
(shipped in 9cff96a).

## Readiness recommendation

**ready_for_tier2** on founder sign-off. Rationale: law layer verified; universe clean
with zero model-gap blockers; bundle map complete; platform layer needs one new
must-build (BoardDocs) + one generic crawler, the rest reuse/per-onboarding. First
Tier-2 batch already has a head start — the Dorchester bundle (county + DD2 + DD4) is
fully reconned (Granicus + BoardDocs/Diligent + NextRequest), and the original
adjacency rationale (Aiken/Edgefield/North Augusta CSRA spillover from GA) remains the
standing structural queue logic.
