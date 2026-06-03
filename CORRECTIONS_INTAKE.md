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
