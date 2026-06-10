"""
Yearly budget automation — scrape + "translate" each jurisdiction's adopted
annual budget.

Source (v1): TED, the UGA Carl Vinson Institute statewide repository of GA local
government budget reports (one source covering every city and county; school
districts file through GaDOE and are a separate provider, TODO). A jurisdiction
opts in via config:

    "platform_hints": { "budget_source": {
        "provider": "ted", "ted_type": "county", "ted_slug": "columbia" } }

The resolver probes TED for the latest filed budget report; the extractor pulls
the top line (totals + by-fund/by-department breakdown + a plain-language
summary) into government_budget. One row per (jurisdiction, fiscal_year);
idempotent (skips a FY already stored unless --force); fund-gated; failures
recorded — same guardrails as the rest of the pipeline.

    python -m townwatch_etl.jobs.extract_budgets --jurisdiction columbia-county-ga
    python -m townwatch_etl.jobs.extract_budgets --all
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from typing import Any

from ..db import connect
from .. import funds
from ..http_client import civic_get, civic_request
from ..jurisdiction import load_config, jurisdiction_fips, list_slugs
from ..extractors.budgets import extract_budget
from .pipeline_errors import record_process_error as _record_process_error

_TED_BASE = (
    "https://ted.cviog.uga.edu/financial-documents/sites/default/files//"
    "budgetdoc/budget-report/"
)

# Which governing body adopts the budget, by jurisdiction type.
_ADOPTING_BODY_TYPE = {
    "city": "city_council", "town": "city_council", "village": "city_council",
    "county": "county_commission",
    "school_district": "board_of_education",
}

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _resolve_ted(ted_type: str, ted_slug: str, *, max_year: int, lookback: int = 8):
    """Probe TED for the latest available budget report. TED fyNNNN files are
    named {type}-{slug}-fyYYYY-budget-report.pdf; we walk years downward from
    max_year and return the newest that exists. Returns (url, fiscal_year)|None."""
    for yr in range(max_year, max_year - lookback, -1):
        url = f"{_TED_BASE}{ted_type}-{ted_slug}-fy{yr}-budget-report.pdf"
        try:
            r = civic_request("HEAD", url, timeout=20)
            if r.status_code == 200 and "pdf" in (r.headers.get("content-type") or "").lower():
                return url, yr
        except Exception:
            continue
    return None


def _jurisdiction_and_body(conn, cfg) -> tuple[int | None, int | None]:
    fips = jurisdiction_fips(cfg)
    row = conn.execute(
        "SELECT id, jurisdiction_type FROM jurisdiction WHERE fips_code = %s", (fips,),
    ).fetchone()
    if not row:
        return None, None
    jid = row["id"]
    body_type = _ADOPTING_BODY_TYPE.get(row["jurisdiction_type"])
    bid = None
    if body_type:
        b = conn.execute(
            "SELECT id FROM governing_body WHERE jurisdiction_id = %s AND body_type = %s "
            "ORDER BY id LIMIT 1", (jid, body_type),
        ).fetchone()
        bid = b["id"] if b else None
    return jid, bid


def _process(slug: str, *, force: bool) -> str:
    cfg = load_config(slug)
    src = (cfg.get("platform_hints") or {}).get("budget_source")
    if not src:
        return "no_source"
    if src.get("provider") != "ted":
        # school districts (GaDOE) and other providers are not wired yet.
        print(f"  · {slug}: budget provider {src.get('provider')!r} not supported yet")
        return "unsupported_provider"

    resolved = _resolve_ted(
        src["ted_type"], src["ted_slug"], max_year=date.today().year + 1,
    )
    if resolved is None:
        print(f"  · {slug}: no TED budget report found")
        return "not_found"
    url, fy = resolved

    with connect() as conn:
        jid, bid = _jurisdiction_and_body(conn, cfg)
        if jid is None:
            print(f"  ✗ {slug}: jurisdiction not in DB")
            return "no_jurisdiction"
        if not force:
            existing = conn.execute(
                "SELECT 1 FROM government_budget WHERE jurisdiction_id = %s AND fiscal_year = %s",
                (jid, fy),
            ).fetchone()
            if existing:
                print(f"  ⊘ {slug}: FY{fy} budget already stored")
                return "exists"

    try:
        pdf = civic_get(url, timeout=180.0).content
    except Exception as e:
        print(f"  ✗ {slug}: budget fetch failed: {e}")
        return "fetch_failed"

    with funds.gate(jid, job_name="extract_budgets", ref_kind="jurisdiction",
                    ref_id=str(jid), description="budget extraction", essential=False) as g:
        if g.paused:
            print(f"  ⏸ {slug}: funds paused — deferring budget extraction")
            return "paused"
        try:
            ext, method = extract_budget(pdf)
        except Exception as e:
            print(f"  ✗ {slug}: extraction failed: {type(e).__name__}: {e}")
            return "extract_failed"

    adopted = ext.adopted_date if (ext.adopted_date and _ISO_DATE.match(ext.adopted_date)) else None
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO government_budget (
                jurisdiction_id, governing_body_id, fiscal_year, source_provider, source_url,
                adopted_date, total_revenues, total_expenditures, fund_breakdown,
                department_breakdown, plain_summary, extraction_method, extraction_confidence,
                extracted_at
            )
            VALUES (%s, %s, %s, 'ted', %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, now())
            ON CONFLICT (jurisdiction_id, fiscal_year) DO UPDATE SET
                governing_body_id    = EXCLUDED.governing_body_id,
                source_url           = EXCLUDED.source_url,
                adopted_date         = EXCLUDED.adopted_date,
                total_revenues       = EXCLUDED.total_revenues,
                total_expenditures   = EXCLUDED.total_expenditures,
                fund_breakdown       = EXCLUDED.fund_breakdown,
                department_breakdown = EXCLUDED.department_breakdown,
                plain_summary        = EXCLUDED.plain_summary,
                extraction_method    = EXCLUDED.extraction_method,
                extraction_confidence = EXCLUDED.extraction_confidence,
                extracted_at         = now(), updated_at = now()
            """,
            (
                jid, bid, ext.fiscal_year or fy, url, adopted,
                ext.total_revenues, ext.total_expenditures,
                json.dumps([f.model_dump() for f in ext.funds]),
                json.dumps([d.model_dump() for d in ext.departments]),
                ext.plain_summary, method, ext.extraction_confidence,
            ),
        )
    rev = f"${ext.total_revenues:,.0f}" if ext.total_revenues else "?"
    exp = f"${ext.total_expenditures:,.0f}" if ext.total_expenditures else "?"
    print(f"  ✓ {slug}: FY{fy} budget — rev {rev} / exp {exp} "
          f"({len(ext.funds)} funds, {len(ext.departments)} depts, conf={ext.extraction_confidence})")
    return "ok"


def main() -> int:
    p = argparse.ArgumentParser(description="Scrape + translate adopted government budgets (TED)")
    p.add_argument("--jurisdiction", help="single jurisdiction slug")
    p.add_argument("--all", action="store_true", help="every jurisdiction with a budget_source")
    p.add_argument("--force", action="store_true", help="re-extract a fiscal year already stored")
    args = p.parse_args()
    if not args.jurisdiction and not args.all:
        p.error("specify --jurisdiction <slug> or --all")

    slugs = [args.jurisdiction] if args.jurisdiction else list_slugs()
    print(f"Budgets to process: {len(slugs)} jurisdiction(s)")
    tally: dict[str, int] = {}
    for slug in slugs:
        try:
            out = _process(slug, force=args.force)
        except Exception as e:
            out = "error"
            print(f"  ✗ {slug}: {type(e).__name__}: {e}")
            _record_process_error("extract_budgets", None, e)
        tally[out] = tally.get(out, 0) + 1
    print(f"Done. {tally}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
