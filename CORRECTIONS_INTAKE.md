# Correction-intake service — deploy runbook

The public "Report an error" affordance on the website needs one small, always-on
HTTP service in the ETL domain. It is the **only** part of the system that
accepts a public write; everything else in the ETL is batch, and the web layer
stays strictly read-only (it forwards reports here server-side).

- Service code: `etl/townwatch_etl/api.py` (FastAPI)
- Write logic + triage: `etl/townwatch_etl/corrections.py`
- Table: `data_correction` (migration `032_data_correction.sql`)
- Web forwarder: `townwatch-web` → `app/api/corrections/route.ts`

## What it does

`POST /corrections` records one citizen error report into `data_correction`.
A report is logged with status `open` and **does not** mutate the referenced
datum — only an operator accepting it (`corrections.accept`) flips a motion to
`disputed`. `GET /healthz` is a liveness probe.

## Deploy (Railway)

1. **New service, same repo.** Add a second service in the Railway project,
   sourced from the `townwatch` repo (the ETL container). Root/build is the
   existing `etl/` image — only the start command changes.

2. **Start command** (Procfile `web:` process already provides this):

   ```
   uvicorn townwatch_etl.api:app --host 0.0.0.0 --port $PORT
   ```

   The container sets `PYTHONPATH=/app/etl`, so `townwatch_etl.api` imports
   without a `cd`.

3. **Env vars on the intake service:**
   - `DATABASE_URL` — the same Railway Postgres the batch worker uses.
   - `INTAKE_TOKEN` — a shared secret you generate (optional but recommended;
     when set, requests must carry it as `X-Intake-Token`).

4. **Env vars on the `townwatch-web` service:**
   - `CORRECTIONS_API_URL` — the intake service's public base URL
     (e.g. `https://townwatch-intake.up.railway.app`). No trailing slash needed.
   - `INTAKE_TOKEN` — the same secret as above (the web forwarder attaches it).

5. **Verify:** `curl https://<intake-url>/healthz` → `{"status":"ok"}`. Then use
   the site's "Report an error" form; the report should land in `data_correction`.

Until `CORRECTIONS_API_URL` is set on the web service, the "Report an error"
button is visible but returns a friendly "coming soon" (HTTP 503) — nothing
breaks, the feature is just dormant.

## Triage (operator)

```
python -m townwatch_etl.corrections --list
python -m townwatch_etl.corrections --accept <id> --note "fixed, re-extracted"
python -m townwatch_etl.corrections --reject <id> --note "tally is correct per p.3"
```

Accepting flags the datum `disputed` (pending a re-extract/fix); rejecting just
closes the report. Nothing a citizen submits changes the live record on its own.

## Run locally

```
cd etl
set -a && . ./.env && set +a
INTAKE_TOKEN=local-dev-token .venv/bin/uvicorn townwatch_etl.api:app --port 8787
```

Then in `townwatch-web/.env.local`:

```
CORRECTIONS_API_URL=http://127.0.0.1:8787
INTAKE_TOKEN=local-dev-token
```

(Restart the dev server so it picks up the new env.)

---

# Comment-digest cron (the forum's −12h delivery)

The live forum closes 12 hours before a meeting, when published comments are
compiled, agent-reviewed, and emailed to the records custodian. That delivery
runs as an HOURLY cron — `townwatch_etl.jobs.submit_comments` — NOT part of the
daily worker (a once-daily run would miss an evening meeting's mid-day cutoff).

Deploy as a THIRD Railway service off this repo (alongside the daily worker and
the intake service), pointed at `railway.comments.toml`
(Settings → Config-as-code → `railway.comments.toml`). It sets the start command
and `cronSchedule = "0 * * * *"`.

Env on that service:
- `DATABASE_URL` — shared Postgres.
- `ANTHROPIC_API_KEY` — the agent's final digest review (cheap Haiku).
- `RESEND_API_KEY` + `RESEND_FROM` — outbound email. **Unset = safe no-op**: the
  cron still compiles + reviews and logs what it WOULD send, but emails nothing,
  so it's safe to deploy before email is configured.

The job is idempotent (`meeting.comments_submitted_at` guards a double-send), so
running it every hour is safe. Custodian addresses come from
`jurisdiction.records_custodian_email`, synced from each town's config by
`sync_jurisdictions` — so a newly-onboarded town is covered automatically.

Trigger manually / test:
```
python -m townwatch_etl.jobs.submit_comments --dry-run   # compile + review, don't send
python -m townwatch_etl.jobs.submit_comments             # live
```

Operator triage of any held/flagged digests + pending comments:
```
python -m townwatch_etl.comments --list-pending
```
