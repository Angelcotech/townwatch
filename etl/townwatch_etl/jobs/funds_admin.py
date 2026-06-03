"""
Fund administration — the CLI over townwatch_etl.funds.

Activate the fund gate for a jurisdiction (deposit > 0 creates its fund row),
inspect balances, set the floor, manually pause/resume, and reap stale holds.
A jurisdiction with NO fund row is unmetered/ungated; depositing opts it in.

Examples
  # opt a jurisdiction in + fund it
  python -m townwatch_etl.jobs.funds_admin --jurisdiction columbia-county-ga --deposit 50

  # set the safety floor (pause when available would drop below this)
  python -m townwatch_etl.jobs.funds_admin --jurisdiction columbia-county-ga --set-floor 2

  # check one / list all
  python -m townwatch_etl.jobs.funds_admin --jurisdiction columbia-county-ga --status
  python -m townwatch_etl.jobs.funds_admin --list

  # manual hold / resume; reap holds from crashed runs
  python -m townwatch_etl.jobs.funds_admin --jurisdiction columbia-county-ga --pause "manual hold"
  python -m townwatch_etl.jobs.funds_admin --jurisdiction columbia-county-ga --resume
  python -m townwatch_etl.jobs.funds_admin --release-stale 60
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal

from ..db import connect
from ..jurisdiction import load_config, jurisdiction_fips
from .. import funds


def _jid(conn, slug: str) -> int:
    fips = jurisdiction_fips(load_config(slug))
    row = conn.execute("SELECT id, display_name FROM jurisdiction WHERE fips_code = %s", (fips,)).fetchone()
    if row is None:
        raise SystemExit(f"no jurisdiction with fips for slug '{slug}'")
    return row["id"]


def _print_status(conn, jid: int, label: str) -> None:
    f = funds.get_fund(conn, jid)
    if f is None:
        print(f"  {label}: (no fund — unmetered/ungated)")
        return
    bal = funds.ledger_balance(conn, jid)
    held = funds.reserved_total(conn, jid)
    avail = bal - held
    flag = "" if f["status"] == "active" else f"  [{f['status'].upper()}]"
    print(f"  {label}{flag}")
    print(f"      balance ${bal:.4f}  −  held ${held:.4f}  =  available ${avail:.4f}   (floor ${Decimal(str(f['min_balance_floor'])):.4f})")
    if f["status"] != "active" and f["paused_reason"]:
        print(f"      reason: {f['paused_reason']}")


def main() -> int:
    p = argparse.ArgumentParser(description="Per-jurisdiction fund administration")
    p.add_argument("--jurisdiction", help="jurisdiction slug")
    p.add_argument("--deposit", type=str, help="add USD (creates the fund / opts in)")
    p.add_argument("--set-floor", type=str, help="set the pause floor in USD")
    p.add_argument("--set-reserve", type=str,
                   help="set the operating reserve in USD (protects daily refresh + "
                        "comment moderation from discretionary spend)")
    p.add_argument("--pause", type=str, metavar="REASON", help="manually pause processing")
    p.add_argument("--resume", action="store_true", help="resume after a pause")
    p.add_argument("--status", action="store_true", help="show this jurisdiction's fund")
    p.add_argument("--list", action="store_true", help="list all funded jurisdictions")
    p.add_argument("--release-stale", type=int, metavar="MIN",
                   help="release reservations older than MIN minutes (janitor)")
    p.add_argument("--no-trigger", action="store_true",
                   help="don't auto-start a pipeline run after a deposit")
    args = p.parse_args()

    with connect() as conn:
        if args.release_stale is not None:
            n = funds.release_stale(conn, args.release_stale)
            print(f"released {n} stale reservation(s) older than {args.release_stale}m")
            return 0

        if args.list:
            rows = conn.execute(
                "SELECT f.jurisdiction_id, j.display_name FROM jurisdiction_fund f "
                "JOIN jurisdiction j ON j.id = f.jurisdiction_id ORDER BY j.display_name"
            ).fetchall()
            if not rows:
                print("no funded jurisdictions yet")
                return 0
            print(f"=== {len(rows)} funded jurisdiction(s) ===")
            for r in rows:
                _print_status(conn, r["jurisdiction_id"], r["display_name"])
            return 0

        if not args.jurisdiction:
            p.error("--jurisdiction is required (or use --list / --release-stale)")
        jid = _jid(conn, args.jurisdiction)

        deposited = args.deposit is not None and Decimal(args.deposit) > 0
        if args.deposit is not None:
            lid = funds.deposit(conn, jid, Decimal(args.deposit), ref_kind="manual",
                                description="admin deposit")
            print(f"deposited ${Decimal(args.deposit):.2f} (ledger #{lid})")
            # Funding a town "builds" it — seed the operating reserve so daily
            # refresh + comment moderation are protected from discretionary spend.
            funds.ensure_reserve_default(conn, jid)
        if args.set_floor is not None:
            funds.set_floor(conn, jid, Decimal(args.set_floor))
            print(f"floor set to ${Decimal(args.set_floor):.2f}")
        if args.set_reserve is not None:
            funds.set_reserve(conn, jid, Decimal(args.set_reserve))
            print(f"operating reserve set to ${Decimal(args.set_reserve):.2f}")
        if args.pause is not None:
            # suspend = manual hold that auto-resume won't clear
            funds.ensure_fund(conn, jid)
            conn.execute(
                "UPDATE jurisdiction_fund SET status='suspended', paused_reason=%s, "
                "paused_at=now(), updated_at=now() WHERE jurisdiction_id=%s",
                (args.pause, jid))
            print(f"suspended: {args.pause}")
        if args.resume:
            conn.execute(
                "UPDATE jurisdiction_fund SET status='active', paused_reason=NULL, "
                "paused_at=NULL, updated_at=now() WHERE jurisdiction_id=%s", (jid,))
            print("resumed (status=active)")

        _print_status(conn, jid, args.jurisdiction)

    # After the deposit COMMITS, kick off a pipeline run for this jurisdiction
    # (onboarding / current-state audit + resume of pending work) unless one is
    # already running. Outside the connection block so the spawned run sees the
    # funds. This is the deposit → work-starts-now behaviour.
    if deposited and not args.no_trigger:
        from .daily_refresh import trigger
        trigger(args.jurisdiction)
    return 0


if __name__ == "__main__":
    sys.exit(main())
