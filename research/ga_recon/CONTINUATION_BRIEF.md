# GA Recon — Continuation Brief (round 2: Boards of Education + public-comment routing)

You are continuing a structured recon of Georgia jurisdictions for TownWatch. This brief is
self-contained. Read it, then read the three artifacts in **§1**, then do the work in **§4–§5**.

## 0. What TownWatch is (one paragraph, so your judgment calls land right)

TownWatch is non-partisan public-records infrastructure: it ingests each local government's
published meeting records (agendas, minutes, rosters, budgets), audits them against open-records
/ open-meetings law, and routes citizen public comment to the right place. Onboarding a
jurisdiction is cheap and automatable IF we already know *where* it publishes and *how* it takes
comments/records requests. This recon builds that map **before** onboarding, so triage knows the
cost up front. You are producing **reference DATA, not code and not configs.**

## 1. Read these first (authoritative formats — do not deviate from them)

- `research/ga_recon/METHODOLOGY.md` — the controlled vocabularies and per-jurisdiction procedure.
  **Obey it.** Especially the honesty rule in §2.5: *a verified entry beats ten guesses.*
- `research/ga_recon/registry.json` — the file you append to. `schema_version`, `registry_meta`
  (incl. `field_vocabularies`), and `jurisdictions` (a dict keyed by slug). Study an existing
  entry (e.g. `columbia-county-ga`) for the exact field set.
- `jurisdictions/_jurisdiction.schema.json` + `jurisdictions/_open_records_laws.json` — the eventual
  config shape an entry graduates into. You don't write configs, but your fields should map cleanly.

## 2. Where round 1 left off (the gaps you're filling)

Current registry: **33 entries — 20 counties, 11 cities, 1 school district, 1 consolidated.**
Two concrete gaps:

1. **Boards of Education are almost entirely missing** (only Columbia County School District).
   GA has ~180 school systems (159 county + ~21 independent city). **This is the priority.**
2. **There is no public-comment dimension at all.** Round 1 captured agenda publishing + records
   intake, but not *where public comment goes per meeting type* or *which comment service* a
   jurisdiction uses. You are adding that dimension (**§5**).

## 3. The unit of work + ordering

- The unit of work is **the jurisdiction** — each gets one complete entry, not three parallel lists.
- **Local-first, and BoE-first this round.** Suggested order: (a) the Boards of Education for
  counties/cities already in the registry (so onboarded/known areas get their school system), then
  (b) net-new BoEs by population, then (c) continue cities/counties as capacity allows.
- Consolidated governments (Augusta–Richmond, Athens–Clarke, Columbus–Muscogee, Macon–Bibb) are
  ONE entity, not two.

## 4. Task A — extend coverage (follow METHODOLOGY.md exactly)

For each jurisdiction, produce the **existing** entry fields (see the `columbia-county-ga` entry):
`name, type, batch, county, county_seat (n/a for BoE), official_website, agenda_platform,
agenda_source_url, records_custodian_{name,title,email}, clerk_email_access, records_intake,
records_intake_url, notable_gaps[], verified, confidence, notes`.

- `type` for school systems = `"school_district"`.
- **BoE platform reality (use as priors, but VERIFY on the live site):** school boards skew
  `boarddocs` (go.boarddocs.com/ga/...), `simbli_eboard` (simbli.eboardsolutions.com — a GSBA
  product, very common in GA), and `edlio` (apps/pages/index.jsp — agendas often missing → that's
  an audit finding, mark `none_found` if truly unpublished). Records intake often `nextrequest` or
  `email`. **Source list for the universe:** GA Dept. of Education system directory (gadoe.org);
  NCES (LEAID). Cross-check the board's own site for the live platform.
- Honor the vocab in `registry_meta.field_vocabularies`. If you hit a platform not in the vocab,
  add the value to that vocab list in `registry_meta` AND use it (don't invent silently).

## 5. Task B — the NEW `public_comment` dimension (add to EVERY entry)

Add a `public_comment` object to each entry. Comment routing can differ **by body and meeting
type** (a council regular meeting may take email-to-clerk; a zoning hearing a sign-up form; a BoE
meeting a BoardDocs eComment), so capture jurisdiction defaults **plus** `by_body` overrides.

```jsonc
"public_comment": {
  "channel": "email | web_form | portal_ecomment | sign_up_card | in_person_only | none_found | unknown",
  "comment_service": "boarddocs_ecomment | granicus_speakup | civicclerk | simbli | swagit | municode | custom_form | email | none | unknown",
  "submit_url": "<live URL or null>",
  "recipient_email": "<email or null>",          // when channel = email
  "deadline_rule": "<e.g. 'by 5:00pm the business day before' or null>",
  "max_comment_chars": <int or null>,
  "accepts_attachment": true | false | null,
  "automatable": true | false | null,            // can a bot submit without CAPTCHA/login/ToS block?
  "captcha_or_tos": "<note on CAPTCHA / login / ToS posture, or null>",
  "by_body": [                                     // only when routing differs from the defaults above
    { "body": "Board of Education", "meeting_types": ["regular"], "channel": "portal_ecomment",
      "comment_service": "boarddocs_ecomment", "submit_url": "...", "recipient_email": null,
      "notes": "eComment opens 48h before, closes at meeting start" }
  ],
  "verified": "verified | inferred",
  "confidence": "high | medium | low",
  "notes": "<where you found this; anything ambiguous>"
}
```

Rules:
- Same honesty discipline as everything else — `verified` only if you saw it live; never fabricate
  a `submit_url` or `recipient_email`.
- `automatable` is a real judgment for our delivery layer: `false` for CAPTCHA / account-required /
  ToS-prohibited portals; `true` for plain email or an open form.
- TownWatch's comment model is **record-first** (we keep a permanent public record of each comment
  and deliver it to the official channel; we do NOT build open forums). You're only recording WHERE
  comment goes and HOW — not designing the UX. If a jurisdiction has no public-comment mechanism,
  `channel: "none_found"` is itself a useful finding.
- Backfill `public_comment` onto the **existing 33 entries** too — second priority, after new BoEs.
  At minimum backfill the three already-onboarded ones (grovetown-ga, columbia-county-ga,
  columbia-county-school-district-ga).

## 6. Output — exactly how to write it

- **Append/extend `research/ga_recon/registry.json` only.** New entries go under `"jurisdictions"`,
  keyed by slug (`{name-slug}-{state}`, e.g. `richmond-county-board-of-education-ga`). Add the
  `public_comment` object to new and existing entries.
- Bump `schema_version` to `0.2`. In `registry_meta`: update `generated_at`, add a
  `field_vocabularies.public_comment_channel` and `.comment_service` list (the vocabularies above),
  and append a one-line `changelog` note ("0.2: added public_comment dimension; BoE coverage").
- **Do NOT** modify `jurisdictions/*.json`, the schema, or any code. Recon graduates into a config
  only when a jurisdiction is actually onboarded — not your job here.
- After writing, validate: the file must `json.load` cleanly, every entry must use only vocab values
  (or values you added to `registry_meta.field_vocabularies`), and every `verified:"verified"` must
  correspond to a value you actually saw on the live site.

## 7. Guardrails (don't drift)

- Non-partisan, infrastructure framing only. Never editorialize about a jurisdiction.
- Recon DATA only: no onboarding, no scrapers, no config writes, no DB writes.
- Honesty over coverage: 10 `verified high` entries beat 50 `inferred low` guesses. Mark uncertainty.
- If you discover the entry schema needs a new field beyond `public_comment`, add it to a
  `## Proposed schema additions` note at the end of this file rather than restructuring silently.

## 8. Deliverable checklist

- [ ] BoEs added (priority), local/known-area-first, following METHODOLOGY.md vocab + honesty rules.
- [ ] `public_comment` object on every new entry; backfilled on the 3 onboarded ones (min).
- [ ] `registry.json` valid JSON; `schema_version` 0.2; `registry_meta` vocab + changelog updated.
- [ ] No config/schema/code files touched.
- [ ] A short summary at the top of your session output: counts added by type, and any jurisdictions
      flagged `none_found` (agenda or comment) as audit findings.
</content>
