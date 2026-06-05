# Native App Direction (iOS / App Store)

> Decision captured 2026-06-05. Strategic direction, not yet built. Shapes how we
> build the web + backend now so the native app is cheap to add later.

## Decision

- **TownWatch ships a native iOS app** (App Store), in addition to the web app.
- **Timeline:** near-term but *not immediate* — after web launch-readiness.
- **Approach: Path B — a real native app over the shared backend** (React Native /
  Expo consuming an HTTP API). NOT a Capacitor/WebView wrapper (Apple rejects thin
  "website-in-a-box" apps under Guideline 4.2), and NOT an Expo-universal rewrite
  that throws away the Next.js investment.
- Web front end (Next.js) deploys now and remains the backbone; the native app is
  a *second front end over the same backend*.

## Why native (not just a wrapper / PWA)

- **Push notifications are the killer feature** — "your council votes tonight,"
  "new finding in your town," "election in 9 days, register by Friday." iOS web /
  PWAs can't do push well; native can. This is a real engagement engine and lines
  up with the elections/voting demand hook and the audit-findings stream.
- App Store presence = distribution + credibility.
- Gives **`townwatch.app`** an obvious job (app landing page, universal/deep links).

## Architectural implications (act on these as we build)

1. **A read-API becomes a required workstream.** Today the **web reads Postgres
   directly** (`lib/db` + server components) and the Intake FastAPI service only
   handles *writes*. A native app cannot reach the DB directly — it needs HTTP read
   endpoints. So before/with native we add a **read API** (either expand the Intake
   service with read endpoints, or Next.js route handlers the app calls). Keep web
   query logic (`lib/queries`, `lib/*-queries`) shaped so it can be mirrored by API
   endpoints without a rewrite. Don't build it yet — but don't design anything that
   makes it harder.
2. **Auth ports via Clerk's Expo SDK** — same user system across web + native, so
   accounts/onboarding/home-jurisdiction carry over.
3. **Push needs a dispatch service** (APNs) — a new backend job that turns events
   (new finding, upcoming vote, election date) into device notifications. Net-new,
   tied to the elections/findings work. The pre-launch lock (`proxy.ts`) is
   web-only; native gates itself via Clerk auth in-app.
4. **Apple compliance:** the app must carry genuine native value (push at minimum)
   to pass review — which Path B delivers by definition.

## Sequencing

1. **Now:** deploy the web front end (Railway), reach launch-readiness, stay
   private behind the pre-launch lock.
2. **After web launch:** stand up the read-API + Clerk Expo auth, build the native
   shell, add push (APNs) wired to findings/elections events.
3. Keep the backend (Postgres + Intake API) as the single shared source for both
   front ends.
