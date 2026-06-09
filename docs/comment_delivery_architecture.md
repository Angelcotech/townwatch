# Public-comment delivery architecture (national scale)

Status: design committed, implementation pending.
Scope: how TownWatch collects, records, and delivers public comment for every
governing body it covers — designed from day one to run unattended across
~19,000+ jurisdictions without a human in the per-jurisdiction loop.

This supersedes the pilot-era assumption baked into `submit_comments.py` that
every body has an emailable records custodian. That assumption silently drops
comment for form-only bodies (e.g. school boards) and does not scale.

---

## 1. The premise: scale by platform, never by jurisdiction

19k jurisdictions do not run 19k systems. They cluster onto a few dozen civic
platforms (BoardDocs/Diligent, Granicus, CivicPlus/CivicClerk, Simbli/eBOARD,
NextRequest) plus plain email. So:

- The unattended unit of work scales with **platforms (dozens)** and the
  **email backbone**, not with jurisdictions (tens of thousands).
- **No human is ever in the per-jurisdiction submission loop.** Anything that
  requires an operator to act per jurisdiction is disqualified by design.
- **We never bypass a CAPTCHA or violate a platform's terms.** At this scale you
  could not legitimately defeat tens of thousands of bespoke forms anyway — so
  no design may make coverage *depend* on getting through one.

## 2. The inversion: the record is the deliverable, delivery is best-effort

The load-bearing decision. For every meeting of every body, TownWatch publishes
a **permanent, timestamped, read-only public comment record** (the permalink):
all published comments + the support/oppose/neutral tally. This is pure data —
DB rows and a rendered page — so it scales without limit and **always exists,
regardless of whether any board's intake channel accepted it.**

Delivery into a board's own channel is then a *secondary, best-effort* push over
**automated channels only**. "TownWatch manages the submission" means TownWatch
**owns and publishes the canonical record and automates delivery over whatever
sanctioned channel each platform offers** — not that a person operates a portal.

The public record is also read-only by mandate: anyone may *read* every comment
and the tally; nobody threads onto anyone else. (See the standing "no open
forum" constraint — bounded windows yes, persistent discussion no.)

## 3. Delivery tiers (a channel resolver, all tiers automated)

Each body resolves to exactly one channel, derived from its config
(`public_comment.intake_method` + platform), never from per-jurisdiction code:

| Tier | Channel | Coverage | Mechanism |
|------|---------|----------|-----------|
| 1 | `email` | Long tail + most councils/commissions | Resend digest to records-email. O(1)/meeting. |
| 2 | `platform_api:<name>` | Large districts on a sanctioned eComment/API platform | One adapter per platform covers thousands of bodies. |
| 3 | `notification` | Form-only, no automatable submit, but some address exists | Automated email to board office / info@ / portal pointing at the permalink + PDF. |
| 4 | `none` | No machine channel at all | Record stands; emit a compliance **finding** ("no scalable public-comment intake"). |

A form-only body is Tier 2 **only if** the platform exposes a sanctioned
endpoint AND there is no CAPTCHA / anti-automation term; otherwise it degrades to
Tier 3. The public record at the permalink is identical across all tiers, so
**coverage never depends on the tier.**

## 4. Compartmentalization (each district / comment section is its own)

Run the project's 4-question compartmentalization test:

1. **Own module path** — all delivery code lives under
   `etl/townwatch_etl/comment_delivery/` (resolver + `adapters/<platform>.py`).
   Nothing else imports into it; it exposes one entry point to the pipeline.
2. **No internal cross-imports** — every adapter implements one common
   `Channel` interface (`capabilities()`, `submit(packet) -> Receipt`). Adapters
   never import each other. Adding BoardDocs cannot touch Granicus.
3. **Data-extension-only for jurisdictions** — onboarding a district is a config
   JSON edit (its `public_comment` block), never a code change. 19k districts =
   19k config rows, one code path.
4. **Records failures** — every unit's failure is persisted and isolated.

**Each "comment section" is an isolated unit of (meeting × body).** It has its
own record, its own resolved channel, its own delivery state, its own retry
clock, and its own failure log. One district's broken form, missing email, or
adapter exception **cannot** block, delay, or corrupt any other district's
section. The pipeline iterates units; a unit that raises is caught, recorded,
and skipped — the batch continues. No shared mutable state across units.

## 5. Persistence in the automation pipeline (durable, idempotent, resumable)

The current `comment_digest` row is a near-fit but its `no_recipient` terminal
state is a *silent drop*. Replace/extend it with a per-section delivery ledger
so the pipeline has durable memory.

### State per comment section (one row per meeting × body)

```
comment_section_delivery
  id, meeting_id, body_id, jurisdiction_id
  permalink_url            -- the public record (set as soon as published)
  record_published_at
  channel                  -- email | platform_api:<name> | notification | none
  state                    -- see machine below
  content_hash             -- hash of the compiled packet
  idempotency_key          -- UNIQUE (meeting_id, body_id, content_hash)
  attempts, last_attempt_at, next_attempt_at   -- retry/backoff
  receipt                  -- provider message/submission id on success
  last_error
  created_at, updated_at
  UNIQUE (meeting_id, body_id)
```

### State machine

```
pending ──► published ──► delivering ──► delivered      (terminal, success)
                    │                └──► notified       (terminal, Tier 3)
                    │                └──► no_channel      (terminal, Tier 4 → finding)
                    └──────────────────► failed_retryable ──(backoff)──► delivering
                                                       └──(attempts exhausted)──► failed
```

### Guarantees

- **No silent drops.** Every unit ends in an explicit, persisted state.
  `no_recipient` is gone; its cases become `notified`, `no_channel` (+finding),
  or `failed`.
- **Idempotent.** The `idempotency_key` + a `delivering` in-flight guard mean
  re-running the submit step — even concurrently, even after a crash mid-batch —
  never double-posts to a board. (Directly prevents the duplicate-submission
  class of incident: re-submitting while a batch is in-flight.)
- **Resumable.** All state lives in the DB. A killed/restarted run picks up every
  unit exactly where it was; `published` records never need regeneration.
- **Retry with backoff.** Transient failures (network, 5xx, rate-limit) set
  `next_attempt_at`; the next pipeline tick retries until a capped attempt count,
  then `failed` (surfaced in the admin failure queue). Permanent failures fail
  fast.
- **Record persists independently of delivery.** `published` is reached and the
  permalink is live *before* any delivery attempt, so the public record survives
  every downstream outcome.

## 6. Pipeline flow (unattended)

Per meeting entering its bounded-window close (e.g. T−12h), for each body:

1. **Publish the record** — upsert the comment section, render/refresh the
   permalink, set `published`. Always succeeds; this is the deliverable.
2. **Resolve channel** from the body's config (Tiers §3). Idempotent.
3. **Compile the packet** — summary within the channel's `max_comment_chars`,
   the permalink, and the full set as a PDF when `accepts_attachment`.
4. **Deliver** via the resolved adapter; persist `state` + `receipt` /
   `next_attempt_at` / `last_error`.
5. **Isolate + record** any unit failure; continue the batch. Tier 4 emits a
   compliance finding.

## 7. Config additions (per body, data-only)

```jsonc
"public_comment": {
  "intake_method": "web_form",          // email | platform_api | web_form | none
  "platform": "boarddocs",              // resolves the Tier-2 adapter, if any
  "request_form_url": "https://…",
  "max_comment_chars": 1500,            // drives packet compaction
  "accepts_attachment": true,           // PDF escape hatch
  "automatable": false                  // false ⇒ degrade to notification (Tier 3)
}
```

## 8. Rollout

- **Pilot (now, GA):** Tier 1 email for city/county; Tier 3 notification for
  Columbia County School District (form-only, no email). Ship the ledger +
  resolver + email/notification adapters + the public record page.
- **National:** add Tier-2 platform adapters one platform at a time, each
  unlocking thousands of bodies. No per-jurisdiction work.
- **On demand:** records are hosted everywhere; the delivery push only fires
  where citizens actually comment — the active set is bounded by demand, though
  the automation holds even if every jurisdiction activated at once.

## 9. Code touch-points (implementation map)

- `townwatch/migrations/04X_comment_section_delivery.sql` — the ledger table.
- `townwatch/etl/townwatch_etl/comment_delivery/` — new compartmentalized package:
  `resolver.py`, `packet.py`, `channel.py` (interface), `adapters/email.py`,
  `adapters/notification.py`, `adapters/<platform>.py`.
- `townwatch/etl/townwatch_etl/jobs/submit_comments.py` — refactor to iterate
  isolated units through the resolver/adapters with the persisted state machine.
- `townwatch-web` — the permanent per-meeting comment-record page (the permalink)
  + surface any `no_channel` finding like other findings.
</content>
