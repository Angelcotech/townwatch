"""
Yearly budget automation — scrape + "translate" each jurisdiction's adopted
annual budget.

Two sources, in precedence order:

  1. LOCAL MEETING RECORD (primary). A jurisdiction adopts its budget by ordinance
     at a meeting we already scrape; the budget document rides in that meeting's
     agenda packet. We find budget-adoption meetings the same way the audit
     observer does (a PASSED motion whose title matches adopt + budget, not an
     amendment), then extract the budget from that meeting's document. Needs NO
     per-jurisdiction config and is current even when the state filing lags.

  2. TED (gap-fill). The UGA Carl Vinson statewide repository of filed GA budget
     reports — clean standardized books, but often years behind. Used to fill
     fiscal years the local record doesn't cover. Opt-in per jurisdiction via
     platform_hints.budget_source {provider:ted, ted_type, ted_slug}.

Precedence is GAP-FILL: neither provider overwrites the other's fiscal year
(local keeps its currency, TED keeps its clean standardized books) — each just
fills years the other lacks. For the same fiscal year from local, the NEWEST
adoption meeting wins (final reading over first reading). --force overrides.
One row per (jurisdiction, fiscal_year) in government_budget. Idempotent
(meeting.budget_extracted_at stamps each adoption meeting once; TED skips a FY
already covered), fund-gated, failures recorded.

    python -m townwatch_etl.jobs.extract_budgets --all
    python -m townwatch_etl.jobs.extract_budgets --jurisdiction grovetown-ga
    python -m townwatch_etl.jobs.extract_budgets --all --source ted
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
from .. import document_text
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

# The budget-adoption motion signature — same logic the audit observer uses to
# detect that a budget WAS adopted: a passed motion to adopt a budget, excluding
# amendments/adjustments.
_ADOPT_WHERE = (
    "mo.outcome = 'passed' AND mo.title ~* 'adopt' AND mo.title ~* 'budget' "
    "AND mo.title !~* 'amend|adjust'"
)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# --------------------------------------------------------------------------- #
# Shared upsert + precedence
# --------------------------------------------------------------------------- #

def _should_write(conn, jid: int, fy: int, provider: str, meeting_date) -> bool:
    """Gap-fill precedence: a fiscal year already populated by the OTHER provider
    is left alone (each provider only fills years the other lacks). For two local
    budgets of the same fiscal year, the newer adoption meeting wins. (--force,
    handled by callers, bypasses this.)"""
    row = conn.execute(
        "SELECT gb.source_provider AS provider, m.meeting_date AS src_date "
        "FROM government_budget gb LEFT JOIN meeting m ON m.id = gb.source_meeting_id "
        "WHERE gb.jurisdiction_id = %s AND gb.fiscal_year = %s",
        (jid, fy),
    ).fetchone()
    if row is None:
        return True
    if provider == "local_minutes" and row["provider"] == "local_minutes":
        # same provider: the newer adoption meeting (final reading) wins
        return meeting_date is not None and (row["src_date"] is None or meeting_date >= row["src_date"])
    # gap-fill: a fiscal year already populated (by either provider, incl. an
    # earlier TED run) is left alone — fill empty years, don't re-extract.
    return False


def _upsert(conn, *, jid, bid, fy, provider, source_url, source_meeting_id, ext, method):
    adopted = ext.adopted_date if (ext.adopted_date and _ISO_DATE.match(ext.adopted_date)) else None
    conn.execute(
        """
        INSERT INTO government_budget (
            jurisdiction_id, governing_body_id, fiscal_year, source_provider, source_url,
            source_meeting_id, adopted_date, total_revenues, total_expenditures,
            fund_breakdown, department_breakdown, plain_summary, extraction_method,
            extraction_confidence, extracted_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, now())
        ON CONFLICT (jurisdiction_id, fiscal_year) DO UPDATE SET
            governing_body_id    = EXCLUDED.governing_body_id,
            source_provider      = EXCLUDED.source_provider,
            source_url           = EXCLUDED.source_url,
            source_meeting_id    = EXCLUDED.source_meeting_id,
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
            jid, bid, fy, provider, source_url, source_meeting_id, adopted,
            ext.total_revenues, ext.total_expenditures,
            json.dumps([f.model_dump() for f in ext.funds]),
            json.dumps([d.model_dump() for d in ext.departments]),
            ext.plain_summary, method, ext.extraction_confidence,
        ),
    )


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


def _infer_fy(meeting_date) -> int:
    """Fallback fiscal year when the document didn't state one. GA local FY begins
    July 1; an original budget adopted in spring/early summer is for the FY that
    begins that July (labeled by the year it ends)."""
    return meeting_date.year + 1 if meeting_date.month >= 4 else meeting_date.year


# --------------------------------------------------------------------------- #
# Local meeting-record provider (primary)
# --------------------------------------------------------------------------- #

def _process_local(slug: str, *, force: bool) -> str:
    cfg = load_config(slug)
    with connect() as conn:
        jid, bid = _jurisdiction_and_body(conn, cfg)
        if jid is None:
            return "no_jurisdiction"
        stamp_clause = "" if force else "AND m.budget_extracted_at IS NULL"
        meetings = [dict(r) for r in conn.execute(
            f"""
            SELECT DISTINCT m.id, m.meeting_date,
                   COALESCE(m.packet_url, m.agenda_url) AS doc_url
            FROM motion mo JOIN meeting m ON m.id = mo.meeting_id
            JOIN governing_body gb ON gb.id = m.governing_body_id
            WHERE gb.jurisdiction_id = %s AND {_ADOPT_WHERE}
              AND COALESCE(m.packet_url, m.agenda_url) IS NOT NULL
              {stamp_clause}
            ORDER BY m.meeting_date DESC
            """,
            (jid,),
        ).fetchall()]
    if not meetings:
        return "no_adoptions"

    outcome = "no_op"
    for mtg in meetings:
        mid, mdate, doc_url = mtg["id"], mtg["meeting_date"], mtg["doc_url"]
        try:
            pdf = civic_get(doc_url, timeout=180.0).content
        except Exception as e:
            print(f"  ✗ {slug} mtg {mid}: fetch failed: {e}")
            outcome = "fetch_failed"
            continue
        with funds.gate(jid, job_name="extract_budgets", ref_kind="meeting",
                        ref_id=str(mid), description="budget extraction (local)", essential=False) as g:
            if g.paused:
                print(f"  ⏸ {slug}: funds paused — deferring")
                return "paused"
            with connect() as conn:
                pages, tmethod = document_text.get_or_recover(conn, pdf, source_url=doc_url)
            if not any(pages):
                print(f"  · {slug} mtg {mid} ({mdate}): no recoverable text ({tmethod}) — skipping")
                with connect() as conn:
                    conn.execute("UPDATE meeting SET budget_extracted_at = now() WHERE id = %s", (mid,))
                outcome = "no_text"
                continue
            try:
                ext, method = extract_budget(pages)
                method = f"{method};text={tmethod}"
            except Exception as e:
                print(f"  ✗ {slug} mtg {mid}: extraction failed: {type(e).__name__}: {e}")
                outcome = "extract_failed"
                continue
        if ext.total_revenues is None and ext.total_expenditures is None:
            # The budget figures weren't in this meeting's document (often the
            # final reading carries only the ordinance, with the detail in a
            # workshop/first reading; some older agendas are scanned with no text
            # layer — OCR fallback is a follow-up). Don't store a figureless row;
            # stamp the meeting so we don't re-spend on it every run.
            print(f"  · {slug} mtg {mid} ({mdate}): no budget figures in this doc — skipping")
            with connect() as conn:
                conn.execute("UPDATE meeting SET budget_extracted_at = now() WHERE id = %s", (mid,))
            outcome = "no_figures"
            continue
        fy = ext.fiscal_year or _infer_fy(mdate)
        with connect() as conn:
            if force or _should_write(conn, jid, fy, "local_minutes", mdate):
                _upsert(conn, jid=jid, bid=bid, fy=fy, provider="local_minutes",
                        source_url=doc_url, source_meeting_id=mid, ext=ext, method=f"local:{method}")
                rev = f"${ext.total_revenues:,.0f}" if ext.total_revenues else "?"
                exp = f"${ext.total_expenditures:,.0f}" if ext.total_expenditures else "?"
                print(f"  ✓ {slug}: FY{fy} from mtg {mid} ({mdate}) — rev {rev} / exp {exp} (conf={ext.extraction_confidence})")
                outcome = "ok"
            else:
                print(f"  = {slug}: FY{fy} from mtg {mid} superseded by a newer/local source")
            # Stamp the meeting either way so it's never re-extracted.
            conn.execute("UPDATE meeting SET budget_extracted_at = now() WHERE id = %s", (mid,))
    return outcome


# --------------------------------------------------------------------------- #
# TED provider (gap-fill)
# --------------------------------------------------------------------------- #

def _resolve_ted(ted_type: str, ted_slug: str, *, max_year: int, lookback: int = 8):
    for yr in range(max_year, max_year - lookback, -1):
        url = f"{_TED_BASE}{ted_type}-{ted_slug}-fy{yr}-budget-report.pdf"
        try:
            r = civic_request("HEAD", url, timeout=20)
            if r.status_code == 200 and "pdf" in (r.headers.get("content-type") or "").lower():
                return url, yr
        except Exception:
            continue
    return None


def _process_ted(slug: str, *, force: bool) -> str:
    cfg = load_config(slug)
    src = (cfg.get("platform_hints") or {}).get("budget_source")
    if not src or src.get("provider") != "ted":
        return "no_ted_source"
    resolved = _resolve_ted(src["ted_type"], src["ted_slug"], max_year=date.today().year + 1)
    if resolved is None:
        return "not_found"
    url, fy = resolved
    with connect() as conn:
        jid, bid = _jurisdiction_and_body(conn, cfg)
        if jid is None:
            return "no_jurisdiction"
        if not force and not _should_write(conn, jid, fy, "ted", None):
            print(f"  ⊘ {slug}: FY{fy} already covered (local or stored) — TED skipped")
            return "exists"
    try:
        pdf = civic_get(url, timeout=180.0).content
    except Exception as e:
        print(f"  ✗ {slug}: TED fetch failed: {e}")
        return "fetch_failed"
    with funds.gate(jid, job_name="extract_budgets", ref_kind="jurisdiction",
                    ref_id=str(jid), description="budget extraction (ted)", essential=False) as g:
        if g.paused:
            return "paused"
        with connect() as conn:
            pages, tmethod = document_text.get_or_recover(conn, pdf, source_url=url)
        if not any(pages):
            print(f"  · {slug}: TED doc has no recoverable text ({tmethod})")
            return "no_text"
        try:
            ext, method = extract_budget(pages)
            method = f"{method};text={tmethod}"
        except Exception as e:
            print(f"  ✗ {slug}: TED extraction failed: {type(e).__name__}: {e}")
            return "extract_failed"
    with connect() as conn:
        if _should_write(conn, jid, ext.fiscal_year or fy, "ted", None):
            _upsert(conn, jid=jid, bid=bid, fy=ext.fiscal_year or fy, provider="ted",
                    source_url=url, source_meeting_id=None, ext=ext, method=f"ted:{method}")
            rev = f"${ext.total_revenues:,.0f}" if ext.total_revenues else "?"
            print(f"  ✓ {slug}: FY{fy} (TED) — rev {rev} (conf={ext.extraction_confidence})")
            return "ok"
    return "exists"


# --------------------------------------------------------------------------- #

def main() -> int:
    p = argparse.ArgumentParser(description="Scrape + translate adopted government budgets")
    p.add_argument("--jurisdiction", help="single jurisdiction slug")
    p.add_argument("--all", action="store_true", help="every jurisdiction")
    p.add_argument("--source", choices=("both", "local", "ted"), default="both")
    p.add_argument("--force", action="store_true", help="re-extract even if already stored")
    args = p.parse_args()
    if not args.jurisdiction and not args.all:
        p.error("specify --jurisdiction <slug> or --all")

    slugs = [args.jurisdiction] if args.jurisdiction else list_slugs()
    print(f"Budgets to process: {len(slugs)} jurisdiction(s) [source={args.source}]")
    tally: dict[str, int] = {}

    def _tick(out: str) -> None:
        tally[out] = tally.get(out, 0) + 1

    for slug in slugs:
        # Local first (primary), then TED to fill fiscal years the local record
        # doesn't cover.
        if args.source in ("both", "local"):
            try:
                _tick("local:" + _process_local(slug, force=args.force))
            except Exception as e:
                _tick("local:error")
                print(f"  ✗ {slug} (local): {type(e).__name__}: {e}")
                _record_process_error("extract_budgets", None, e)
        if args.source in ("both", "ted"):
            try:
                _tick("ted:" + _process_ted(slug, force=args.force))
            except Exception as e:
                _tick("ted:error")
                print(f"  ✗ {slug} (ted): {type(e).__name__}: {e}")
                _record_process_error("extract_budgets", None, e)

    print(f"Done. {tally}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
