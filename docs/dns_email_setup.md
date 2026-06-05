# DNS & Email Setup — townwatch.us / townwatch.app

> Setup reference for TownWatch's email + sending infrastructure. Values marked
> `⟨…⟩` are generated per-account — copy them from the Proton / Resend dashboard
> when you add the domain. Everything else is verbatim.

## Domain roles

| Domain | Role |
|--------|------|
| **townwatch.us** | Canonical brand + website + human mailbox (Proton). The civic-US identity; the address clerks see and reply to. |
| **mail.townwatch.us** | App's transactional sender (Resend) — a subdomain so a spam complaint can't dent the root (human) reputation. |
| **townwatch.app** | 301-redirect → townwatch.us for now; banked for a future logged-in product surface. |

Manage all DNS on **Cloudflare** (free, instant changes, easy `.app` redirect):
point each domain's nameservers at Cloudflare, then add the records below.

## Recommended addresses

| Address | Purpose | Where it lives |
|---------|---------|----------------|
| `requests@townwatch.us` | Records-request / clerk-facing. **Reply-To** on app sends — clerk replies land here. | Proton inbox (root) |
| `hello@townwatch.us` | General contact. | Proton inbox (root) |
| `⟨you⟩@townwatch.us` | Founder personal. | Proton inbox (root) |
| `requests@mail.townwatch.us` | The visible **From** on Resend sends (subdomain). | Resend (no inbox) |

App sends go out **From** `TownWatch <requests@mail.townwatch.us>` with **Reply-To**
`requests@townwatch.us`, so the bulk-send reputation stays on the subdomain but
every reply lands in the human Proton inbox.

---

## 1. townwatch.us — Proton (human mailbox)

| Type | Host / Name | Value | Priority |
|------|-------------|-------|----------|
| TXT | `@` | `protonmail-verification=⟨code⟩` | — |
| MX | `@` | `mail.protonmail.ch` | 10 |
| MX | `@` | `mailsec.protonmail.ch` | 20 |
| TXT (SPF) | `@` | `v=spf1 include:_spf.protonmail.ch ~all` | — |
| CNAME | `protonmail._domainkey` | `⟨target⟩.domains.proton.ch` | — |
| CNAME | `protonmail2._domainkey` | `⟨target⟩.domains.proton.ch` | — |
| CNAME | `protonmail3._domainkey` | `⟨target⟩.domains.proton.ch` | — |
| TXT (DMARC) | `_dmarc` | `v=DMARC1; p=none; rua=mailto:dmarc@townwatch.us; fo=1` | — |

Add the verification TXT and verify ownership in Proton **before** cutting MX over.
Start DMARC at `p=none`; after a clean week, tighten to `p=quarantine`.

## 2. mail.townwatch.us — Resend (app sender)

Add **`mail.townwatch.us`** as the domain in Resend; hosts shown relative to the
`townwatch.us` zone:

| Type | Host / Name | Value | Priority |
|------|-------------|-------|----------|
| MX | `send.mail` | `feedback-smtp.⟨region⟩.amazonses.com` | 10 |
| TXT (SPF) | `send.mail` | `v=spf1 include:amazonses.com ~all` | — |
| TXT (DKIM) | `resend._domainkey.mail` | `p=⟨DKIM key⟩` | — |
| TXT (DMARC) | `_dmarc.mail` | `v=DMARC1; p=none;` | — |

`⟨region⟩` is whatever Resend shows (usually `us-east-1`).

## 3. townwatch.app → redirect

Add `townwatch.app` to Cloudflare, then:

| Type | Host | Value | Proxy |
|------|------|-------|-------|
| A | `@` | `192.0.2.1` (dummy; the rule serves the redirect) | Proxied |
| A | `www` | `192.0.2.1` | Proxied |

**Rules → Redirect Rules:** `301` from hostname `townwatch.app` / `www.townwatch.app`
→ `https://townwatch.us/${path}`. Cloudflare Universal SSL satisfies `.app`'s forced
HTTPS automatically.

---

## Env vars to set after verification

| Var | Service | Value |
|-----|---------|-------|
| `RESEND_API_KEY` | ETL | `re_…` |
| `RESEND_FROM` | ETL | `TownWatch <requests@mail.townwatch.us>` |
| `RESEND_WEBHOOK_SECRET` | Web | `whsec_…` (from the Resend webhook you point at `/api/resend-webhook`) |
| `CORRECTIONS_API_URL`, `INTAKE_TOKEN` | Web | already set (Clerk webhook / comments) — the Resend webhook reuses them |

Resend webhook: subscribe to `email.bounced`, `email.complained`, `email.delivered`
→ those flip clerk-contact health and surface on the dev dashboard automatically.

## Sequence (so nothing breaks)

1. Nameservers → Cloudflare for both domains.
2. Add `townwatch.us` in Proton → verification TXT + MX + SPF + 3 DKIM → verify → test send/receive.
3. Add `mail.townwatch.us` in Resend → its 4 records → verify → set `RESEND_*` env + Reply-To.
4. Resend webhook → `RESEND_WEBHOOK_SECRET` on web.
5. `.app` redirect.
6. After a clean DMARC week, bump both `p=none` → `p=quarantine`.

## Plan note

Proton **Mail Plus** (custom domain + multiple addresses/aliases into one inbox) is
sufficient for a solo founder. **Business** ($/user) is only worth it for separate
teammate logins, a catch-all (`*@townwatch.us`), or storage headroom — upgrade later
with zero migration.
