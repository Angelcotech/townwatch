# The Voting Section — vision & v1 scope

> Status: design draft (2026-06-05). This is the founding insight of TownWatch
> written down, plus a tractable first slice. Not yet built. Treat the principles
> as load-bearing and the v1 scope as the thing to argue with.

## Why this exists

This is the problem that started TownWatch. You're getting ready to vote. You
want to do it well. And it's nearly impossible:

- There are **more races and candidates than anyone can research** — especially
  down-ballot, where the offices that touch your life most (school board, county
  commission, city council) get the least coverage.
- The information that exists is **scattered** across campaign sites, a county
  elections page, a few news clips, and social media — never in one place, never
  comparable side by side.
- The **ballot questions themselves are written in legalese** you'd need a law
  degree to parse, so even when you're standing at the machine you can't tell what
  a YES actually does.
- And for the incumbents asking for your vote, there's a yawning gap between **what
  they campaign on and what they actually did in office** — which is precisely the
  thing no one hands you.

The result: well-intentioned people walk into the booth and guess. The Voting
Section exists to close that gap — to make it *possible to cast an informed vote*
without being a full-time researcher or a lawyer.

## What TownWatch uniquely brings

Voter guides exist. Most are either partisan, paywalled, or shallow. TownWatch has
one thing none of them do: **the actual record.** We already extract how local
bodies voted, who moved which motion, who showed up. So for any incumbent on a
ballot we can show *what they did*, not what they promise — sourced to the minutes,
linked to the meeting.

That's the bridge from the audit to the ballot, and it's the differentiator:

> **"Here's how the person asking for your vote actually voted."**

Nobody has that for local races. We do, as a byproduct of the work already done.

## Principles (non-negotiable)

The Voting Section is the highest-trust, highest-risk surface in TownWatch. The
same doctrine that governs the rest of the project applies with extra force:

1. **Nonpartisan, always.** We never tell anyone how to vote, never rank or score
   candidates, never imply a "right" answer. We present facts and sources and let
   the citizen decide. This is constitutional infrastructure (libraries, the
   Census), not a campaign tool. We refuse partisan adoption.
2. **Plain language, not spin.** Translating legalese is the core value — and the
   sharpest knife. A ballot-measure explainer must state, neutrally, *what a YES
   does and what a NO does*, in factual terms, with the **official text shown
   alongside** and cited. No characterization of which is "good."
3. **Everything is sourced.** Every claim links to its origin — the minutes for a
   vote, the official ballot text for a measure, the SoS/county page for a date.
   If we can't source it, we don't say it.
4. **Records over promises.** For incumbents we lead with the verifiable record.
   Campaign claims, if shown at all, are clearly labeled as the candidate's own
   words, not endorsed.
5. **Human-in-the-loop on anything synthesized.** Any AI-written plain-language
   summary (measures especially) is *drafted, then human-reviewed before publish* —
   the same "build faith before sending" discipline as records requests. An
   un-reviewed AI gloss of a ballot measure is exactly the failure we can't have.
6. **Local-first.** The races people can't research are local. State/federal are
   downstream context, not the focus.
7. **No open forum.** No comment threads, no candidate Q&A free-for-all, no ratings.
   Bounded, factual, read-only. (Consistent with the no-open-forum rule.)
8. **Demand-gated.** Built and activated per jurisdiction on demand, like everything
   else — the calendar is cheap to compute everywhere; the deep dossier activates
   where there's interest.

## The four voter questions (the product, decomposed)

The pain breaks into four distinct, separately-addressable questions:

| # | Question | Hardness | TownWatch advantage |
|---|----------|----------|---------------------|
| A | *What's on my ballot?* (races + measures for my address) | medium — needs district mapping | we already map address→district (`/api/lookup-district`) |
| B | *Who are these candidates?* | medium — scattered, partly new ingestion | thin for challengers; **strong for incumbents** |
| C | *What does this ballot question mean?* | hard — legalese → plain language, neutrally | AI draft + human review + official text |
| D | *What has this incumbent actually done?* | **easy for us, impossible for others** | the existing voting/attendance record |

D is the wedge. It's the cheapest for us (data exists) and the most differentiated.
A and the dates are cheap to compute. B (challengers) and C (measures) are the
expensive, episodic, higher-risk parts — they come later and carefully.

## Data layer

- **Election dates** — *computed, not scraped.* GA election dates follow statutory
  rules (general election dates, runoff windows, qualifying periods, registration
  deadlines, advance-voting windows). A calendar-driven module derives them; we
  don't fragile-scrape a county page. (Matches the elections-module note: compute
  dates, monitor on a cadence.)
- **Races + offices on the ballot** — which seats are up is derivable from our term
  data (seat terms expire on a schedule) for the bodies we cover, plus the SoS
  qualifying list for the rest.
- **Incumbents + their record** — already in our DB (roster + term + motion/vote +
  attendance). This is the dossier's spine.
- **Candidates (challengers)** — GA Secretary of State qualifying lists + county
  elections; new, lightweight ingestion. Start by *listing* qualified candidates
  (factual: name, office, party-as-filed) before attempting any profile.
- **Ballot measures** — official ballot text from the SoS/county; the explainer is
  AI-drafted from that text, human-reviewed, shown beside the original.

## v1 scope (smallest valuable slice)

**v1 = the Election Dossier for a covered jurisdiction, built from data we already
have.** For an upcoming election in a jurisdiction we cover:

1. **Election calendar** — the key dates, computed: registration deadline, advance
   voting window, election day, runoff (if applicable). Cheap, accurate, useful
   everywhere immediately.
2. **What's on the ballot (local)** — the seats up for election in this
   jurisdiction's bodies, derived from term data.
3. **Incumbent accountability card** — for each incumbent whose seat is up: their
   TownWatch record — votes on notable motions, attendance, tenure — each linked to
   the source. *This is the hero feature.* It reuses the voting/roster data wholesale.

Explicitly **out of v1** (deferred, by design):
- Challenger profiles / candidate ingestion (B) — v2; start with a factual
  qualified-candidates *list* before any profile.
- AI plain-language ballot-measure explainer (C) — v2, and only behind human
  review. Highest-risk feature; do it deliberately, not first.
- Address-level "my exact ballot" personalization — v2; v1 is jurisdiction-level.
- Anything predictive, comparative, or score-like — never.

v1 ships real value (dates everywhere + the incumbent record nobody else has)
using almost entirely existing data, while the risky/expensive parts wait their
turn.

## Phasing beyond v1

- **v2 — Candidates & measures.** Qualified-candidate lists (SoS), then incumbent
  vs. challenger side-by-side (record vs. stated positions, clearly labeled), then
  the human-reviewed plain-language measure explainer with official text alongside.
- **v3 — Personalized ballot.** Address → your exact races + measures (reuse
  district lookup), as a read-only "what you'll see at the booth" preview.
- **v4 — Reminders.** Opt-in, factual, nonpartisan date reminders (registration
  closing, advance voting opening). No turnout targeting, no "go vote for" anything.

## Open questions for the founder

1. **v1 hero confirmation:** is the *incumbent accountability card* the right thing
   to lead with, or do you want the *plain-language measure explainer* in v1 despite
   its risk (it's the most emotionally central to your origin story)?
2. **Surface:** does this live as a new top-level `/vote` (or `/elections`) section,
   or as a tab on each jurisdiction alongside the existing Overview/Voting(records)
   tabs? (Note the naming collision: today's "Voting" tab is *council vote records* —
   the citizen-facing voter section may want a clearer name like "Elections" or
   "Your Vote" to avoid confusion.)
3. **Measure explainer review:** who reviews AI-drafted measure summaries before
   publish, and do we show the AI draft at all before review, or hold entirely?
4. **Scope of "incumbent record":** full vote history, or a curated set of
   notable/decisive votes? (Curated is more useful but introduces selection
   judgment we'd need a neutral, documented rule for.)
```
