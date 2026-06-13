# Railway service: `townwatch-forum-tick` (hourly)

The live public-comment forum is the site's centerpiece, and its value
depends on low latency: an agenda often drops a day or two before the
meeting, and the forum is only useful if it's *informed* (per-item proposal
summaries + packet page deep-links) while there's still time to comment.

`railway.toml` configures only ONE service per file — the primary
**`townwatch`** daily cron (`daily_refresh`, `0 6 * * *`). The hourly forum
tick therefore runs as a **second Railway service in the same project**,
pointing at the same repo + Dockerfile. Stood up and deployed 2026-06-13.

## What it runs

`python -m townwatch_etl.jobs.forum_tick` — three steps, then exits (a clean
cron one-shot):
1. `extract_agendas --all --upcoming` — open a forum the moment a new
   upcoming agenda is published.
2. `extract_packets --all --upcoming` — segment the packet into per-item
   proposal summaries + page ranges (what makes the forum *informed*).
3. `submit_comments` — submit the compiled comment digest for any meeting
   past its −12h cutoff.

`daily_refresh` (the `townwatch` service) also runs `extract_packets` as a
nightly **safety net**; this service is the low-latency path on top of it.

## Railway dashboard settings (record of the deployed config)

- **Service name:** `townwatch-forum-tick`
- **Source:** GitHub `Angelcotech/townwatch` (same repo as `townwatch`)
- **Build:** Dockerfile, path `etl/Dockerfile`, root directory = repo root
- **Deploy → Custom Start Command:** `python -m townwatch_etl.jobs.forum_tick`
- **Deploy → Cron Schedule:** `0 * * * *` (hourly)
- **Variables:** same env as `townwatch` — `DATABASE_URL`,
  `ANTHROPIC_API_KEY`, `MISTRAL_API_KEY` (+ any others on the main service).
  Point at the same Railway Postgres.

## Why it's safe to run alongside the daily cron

- **No double-processing.** Both `forum_tick` and `daily_refresh` take the
  per-jurisdiction advisory `run_lock` (`run_lock.py`). If the hourly tick
  collides with the 06:00 run, one holds the lock and the other skips that
  jurisdiction — never both at once.
- **Cheap at idle.** `extract_agendas --upcoming` only touches *new* agendas;
  `extract_packets` is idempotent (`packet_segmented_at` guards re-work).
  Most ticks do almost nothing until an agenda actually drops. Throttle with
  `0 */3 * * *` (every 3h) if spend needs reining in.
- **Self-monitoring.** If a live forum is ever left without proposal context,
  `refresh_pipeline_health._check_forum_unenriched` opens a `forum_unenriched`
  pipeline issue (visible in `/triage-pipeline`) until it's segmented.

## If this service ever stops

The daily `daily_refresh` still segments upcoming packets, so forums stay
informed within ~24h even with the hourly tick down — and the
`forum_unenriched` flag surfaces any gap. Re-deploy this service to restore
hourly latency.
