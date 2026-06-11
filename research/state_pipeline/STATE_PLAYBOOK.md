# State Expansion Playbook

**Status ledger:** `states.json` (same directory) — one row per state/district, phase
status + quirks + queue. **Completed exemplar:** Georgia (`research/ga_recon/`,
sessions of 2026-06-05 and 2026-06-10). Every phase below names its GA artifact
so a future session can imitate rather than reinvent.

The unit of expansion is NOT "a state, fully reconned." It is three tiers with
very different cost profiles. Never serialize GA-depth work across states.

| Tier | Unit | Cost | Trigger |
|---|---|---|---|
| 0 — Universe seeding | national datasets → `jurisdiction_directory` | ~free, automated | run for all states up front |
| 1 — State Dossier | one state | 1–2 agent sessions + human review gate | queue (adjacency-first, demand-reordered) |
| 2 — Jurisdiction recon → onboarding | one jurisdiction (bundle) | per-jurisdiction | demand/funding only |

---

## Tier 0 — Universe seeding (automated, national)

The seed job is already state-parameterized and reads national Census files:

```
PYTHONPATH=etl etl/.venv/bin/python -m townwatch_etl.jobs.seed_jurisdiction_directory --state XX
```

What it does (see job docstring): places (FUNCSTAT A + consolidated balance
records) + counties + unified school districts from the current-vintage
gazetteer; DoDEA exclusion; bundle derivation (county/city school systems,
consolidated city-counties); stale-row cleanup for statutory dissolutions;
coverage linking by FIPS.

**Per-state reconciliation checklist (the only manual part):**
- [ ] Triage every `⚠ no bundle target` warning — each is a naming-convention
      surprise; resolve via constants or a derivation fix, never silence.
- [ ] Add the state's DoDEA base districts to `DODEA_UNSD_GEOIDS` (check the
      unsd file for military-base names; verify against CCD absence).
- [ ] Cross-check the place count against the state's municipal-league
      directory (GMA-equivalent — every state has one) and current TIGERweb
      vintage; explain every delta (new incorporation / dissolution / vintage
      lag). GA precedent: Mulberry (vintage lag), Ranger + Sunny Side
      (statutory dissolutions).
- [ ] Record results in `states.json` (`universe_seeded`, deltas in notes).

**Known model gaps Tier 0 does NOT yet handle** (seed anyway; flag in ledger):
township/MCD states (municipal government = Census COUSUB, not place — the
place file misses most New England towns), two-tier school states (elsd/scsd
gazetteer files not ingested), county-equivalents (VA independent cities,
St. Louis, Baltimore, Carson City), NYC's five counties. These are tracked as
pipeline work in `townwatch-web/BACKLOG.md`; a state whose quirks include them
cannot finish Tier 0 until the model grows that layer.

## Tier 1 — The State Dossier (the repeatable session)

Output directory: `research/{st}_recon/` mirroring GA. Phases run in order;
each ends with an **adversarial verification gate** (independent agents
instructed to refute, not confirm — see Verification discipline below).

### Phase A — Law layer
What: open records act + open meetings act dossier: statute cites, response
deadline, fee regime, **requester residency restriction** (post-*McBurney v.
Young* these are enforceable — VA/TN/AL/AR/DE/NJ/MO priors in the ledger),
agenda/minutes publishing requirements AND timelines, public-comment
requirements (most OMAs don't mandate comment — what does this state say?),
notice requirements, enforcement mechanism (AG mediation? private suit?).
How: deep-research run with citations + 3-vote verification, seeded from
RCFP's Open Government Guide + NFOIC + the statute text itself.
Graduates into: `jurisdictions/_state_defaults/{st}.json` + a row in
`jurisdictions/_open_records_laws.json`.
Exit criteria: every pipeline-relevant rule has a statute cite verified
against the actual statute text; residency posture decided (direct send vs
citizen-proxy lane).
GA artifact: `_state_defaults/ga.json` (predates the playbook).

### Phase B — Universe verification
What: reconcile Tier-0 counts against state authorities; enumerate
consolidations, dependent school systems, county-equivalents; produce the
roster with population/enrollment ranks and coverage diff.
GA artifacts: `research/ga_recon/universe_roster.json` + `UNIVERSE_SOURCES.md`
(structure: verified counts table → findings with vote tallies → canonical
sources per layer → provenance → errata).
Exit criteria: every layer's count reconciles across ≥2 independent
authoritative sources, with every delta explained; canonical source ruled per
layer; roster validates and diffs against the registry.

### Phase C — Structure & relationships
What: the bundle map — which school systems ride which general-purpose
government, consolidations as single governments, dependent boards (bodies
without separate governments — still audited, trivially bundled), any
state-specific layering (boroughs, parishes, townships).
Graduates into: `bundle_fips` derivation (constants/conventions per state) and
ledger quirk confirmations.
Exit criteria: zero unresolved bundle warnings; quirks in `states.json`
flipped from `prior` to `verified` or removed.

### Phase D — Platform census
What: fingerprint the agenda/records/comment platforms for a stratified
sample: ALL of the top-10 population counties + seats + districts, plus a
rural sample (GA showed rural ≠ metro: Simbli everywhere in schools, custom
sites in rural counties). Produces the scraper-investment ranking (GA:
Simbli = 27/33 districts → one client unlocks the state's school boards).
GA artifact: `research/ga_recon/registry.json` batches `csra_footprint`,
`top10_population`, `top10_seats`, `boe_known_areas`, `boe_population` +
`METHODOLOGY.md` (controlled vocabularies — reuse them verbatim; extend the
vocab lists rather than inventing values).
Exit criteria: platform distribution table with verified/inferred honesty
fields; top platform families identified with existing-scraper reuse noted.

### Phase E — Pipeline-delta memo
What: the answer to "what breaks our automation in this state," written BEFORE
any onboarding: new platform families, law-engine rules (Phase A), universe
model gaps (Tier 0 list), comment-delivery lanes, records-intake lanes,
anti-bot posture observed.
Graduates into: `townwatch-web/BACKLOG.md` items (pattern: "Pipeline hardening"
section, 2026-06-10) and the readiness grade in `states.json`.
Exit criteria: a human (founder) review of the memo — this is the explicit
**human gate** before the state is marked `ready_for_tier2`.

## Tier 2 — Jurisdiction recon → onboarding (demand-gated)

Templates, all proven in GA:
- Recon registry entry shape + honesty rules: `research/ga_recon/METHODOLOGY.md`
  (§2.5: *a verified entry beats ten guesses*) + `CONTINUATION_BRIEF.md`.
- Batching: footprint (adjacency) → top population → demand requests.
- Onboarding bundle: county fund covers county + its school system;
  independent city systems ride their city; consolidations are ONE onboarding
  (`bundle_fips` is the machine-readable rule).
- **Documented-absence placeholder onboarding** for no-published-data
  jurisdictions: config with `data_sources.status: not_available` +
  `known_gaps` (statute-cited) — the absence is the record. Exemplars:
  `jurisdictions/glascock-county-consolidated-schools-ga.json`,
  `taliaferro-county-school-district-ga.json`.
- ORR escalation: placeholder first, then a records request whose paper trail
  feeds the placeholder page; non-response upgrades the finding from "not
  published" to "not produced on request." Drafts pattern:
  `research/ga_recon/orr_drafts/`.

## Verification discipline (applies to every phase)

- Adversarial verification: independent agents prompted to REFUTE each claim;
  2-of-3 survival; record vote tallies in the sources doc.
- `verified` means seen on the live official source THIS session; otherwise
  `inferred` + capped confidence. Never fabricate URLs/emails.
- Exact-URL rule: liveness checks use the full exact URL (a truncated URL
  produced GA's one false finding — registry changelog 0.2.2) and must detect
  soft-404s (HTTP 200 "page not found" bodies).
- Errata are appended, never silently rewritten — the research record keeps
  its own corrections visible.
- Wrong-state lookalike domains are endemic (5 found for GA alone): always
  verify entity identity (state + FIPS + address) before trusting a domain.

## Queue policy

- Default order: **adjacency-first** (local-first, expand outward) — SC is
  queue #1 (the Augusta metro spills into Aiken/Edgefield counties), then the
  remaining GA borders (FL, AL, TN, NC).
- Demand signals reorder the queue: adopt-funnel searches/requests from an
  unseeded or un-dossiered state are logged against its ledger row; sustained
  demand jumps the queue. Money never gates the map (mission rule); it gates
  Tier-2 activation only.
- DC is a single-row special case (one government + one school district);
  territories are deferred rows pending mission scope.

## Ledger spec (`states.json`)

Phase status vocabulary: `not_started | in_progress | done | blocked | n_a`.
Per-state fields: `fips`, `phases` (tier0_universe_seeded, A_law_dossier,
B_universe_verified, C_structure_bundles, D_platform_census, E_pipeline_delta,
tier2_recon: {batches, registry_entries, onboarded}), `readiness`
(`unmapped | seeded | dossier_in_progress | ready_for_tier2 | active`),
`queue_position` (null = unqueued), `priority_reason`, `quirks[]`
(`{tag, status: prior|verified|refuted, note}` — priors come from general
knowledge and MUST be confirmed in Phase B/C before relying on them),
`demand_signals[]`, `artifacts{}` (paths), `notes`.
