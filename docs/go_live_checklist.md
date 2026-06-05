# Go-Live Checklist

> The walk-down list for flipping TownWatch from private preview → public. Built up
> while standing up the front end (2026-06). Nothing here blocks the **private,
> allowlisted preview** — these are the steps to do *before* unlocking to the world.
> Companion docs: `dns_email_setup.md`, `native_app_direction.md`.

Legend: 🔒 hard gate (don't launch without) · 🔧 technical · 📋 business/legal

## 1. Clerk → production instance 🔒🔧
The preview runs on the **dev** Clerk instance (`pk_test_`/`sk_test_`, `accounts.dev`).
Public launch needs a **production** instance — it's more than a key swap:
- [ ] Create the Clerk **production** instance (clone dev settings).
- [ ] Add Clerk's DNS records on `townwatch.us` in Cloudflare — Clerk gives ~5 CNAMEs
      (Frontend API `clerk.`, Account Portal `accounts.`, email + 2× DKIM). DNS-only.
- [ ] Swap `pk_live_` / `sk_live_` into `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` +
      `CLERK_SECRET_KEY` on the web service.
- [ ] Reconnect any social logins (Google, etc.) with **production** OAuth credentials
      — Clerk's shared dev OAuth does not work in prod.
- [ ] Re-create the Clerk webhook in the prod instance → `https://townwatch.us/api/clerk-webhook`,
      subscribe `user.created` + `user.updated` → set `CLERK_WEBHOOK_SECRET` (new `whsec_`).

## 2. Flip the pre-launch lock 🔒🔧
- [ ] Set `PRELAUNCH_LOCK=false` on the web (Railway) service. (This is the actual
      "go public" switch — everything else should be done first.)
- [ ] `PRELAUNCH_ALLOWED_USER_IDS` becomes moot once unlocked; can leave or clear.

## 3. Email webhooks wired to the live URL 🔧
(Can be done as soon as the web app is deployed at a reachable URL — doesn't have to
wait for launch, but must be true by launch.)
- [ ] Resend webhook → `https://townwatch.us/api/resend-webhook`, subscribe
      `email.bounced` / `email.complained` / `email.delivered` → set `RESEND_WEBHOOK_SECRET` on web.
- [ ] Confirm `RESEND_FROM` / `RESEND_REPLY_TO` set on the TownWatch worker (done) and do
      a final production send test.

## 4. Domains & DNS 🔧
- [ ] `townwatch.us` pointed at the web deploy (Railway custom domain → Cloudflare CNAME, DNS-only).
- [ ] `townwatch.app` → 301 redirect to `townwatch.us` (Cloudflare redirect rule).
- [ ] Tighten DMARC `p=none` → `p=quarantine` on both `townwatch.us` (Proton) and
      `mail.townwatch.us` (Resend) after a clean reporting week.

## 5. Legal / business wrapper 🔒📋
(The "tomorrow's work" — a genuine launch gate for a for-profit publishing
government-accountability data.)
- [ ] Entity formed (LLC).
- [ ] Terms of Service + Privacy Policy published (site has accounts, comments, contact).
- [ ] Media-liability / E&O insurance considered.
- [ ] Records requests remain **human-sent** (already the case — no auto-send).

## 6. Payments (Stripe) 🔧📋
- [ ] Decide if funding is live at launch. If yes: wire Stripe (the "Fund this" buttons
      are currently inert) before unlocking. If no: launch read-only, add later.

## 7. Content & trust readiness 🔒
- [ ] Initial jurisdictions indexed and presentable (Columbia County, Grovetown, CCSD).
- [ ] Pre-launch re-audit: confirm **no false findings** are showing anywhere (the core
      trust discipline) — dev dashboard "Needs immediate resolve" is clear.
- [ ] About / positioning copy final; nonpartisan framing intact.

## 8. Operational 🔧
- [ ] `daily_refresh` cron running on Railway (TownWatch worker).
- [ ] Dev dashboard reviewed: no unresolved pipeline failures, clerk-contact health green.

---

### Launch-day order
Do 1, 3, 4, 5, (6) first → verify on the live domain while still locked → then 2
(`PRELAUNCH_LOCK=false`) as the final, deliberate switch.
