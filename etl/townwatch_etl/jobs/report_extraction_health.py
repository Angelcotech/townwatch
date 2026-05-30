"""
Extraction health report — the rollout-confidence number, on demand.

Reads the extraction_outcome ledger and prints the success rate (= produced a
record = clean + recovered) sliced by method and jurisdiction, plus the
anomaly-class breakdown. This is the number that says whether automation is
trustworthy enough to onboard the next jurisdictions, and the early-warning on
whether per-town quality stays flat as scale grows.

Run:
    python -m townwatch_etl.jobs.report_extraction_health
    python -m townwatch_etl.jobs.report_extraction_health --days 7
    python -m townwatch_etl.jobs.report_extraction_health --job extract_minutes
"""

from __future__ import annotations

import argparse
import sys

from ..extraction_ledger import health


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=30, help="look-back window (default 30)")
    p.add_argument("--job", help="restrict to extract_minutes or extract_agendas")
    args = p.parse_args()

    h = health(days=args.days, job_name=args.job)
    o = h["overall"]
    total = o["total"] or 0
    if not total:
        print(f"No extraction outcomes recorded in the last {args.days} day(s)"
              f"{' for ' + args.job if args.job else ''}.")
        return 0

    rate = 100.0 * o["produced"] / total
    print(f"=== extraction health — last {args.days}d"
          f"{' (' + args.job + ')' if args.job else ''} ===")
    print(f"  success rate (produced a record): {rate:.1f}%   "
          f"({o['produced']}/{total})")
    print(f"  clean={o['clean']}  recovered={o['recovered']}  failed={o['failed']}")

    print("  --- by method ---")
    for m in h["by_method"]:
        print(f"    {m['method'] or '(none)':<14} {m['success_pct']!s:>6}%  (n={m['total']})")

    print("  --- by jurisdiction ---")
    for j in h["by_jurisdiction"]:
        flag = f"  ⚠ {j['failed']} failed" if j["failed"] else ""
        print(f"    {(j['jurisdiction'] or '(unknown)'):<28} {j['success_pct']!s:>6}%  (n={j['total']}){flag}")

    if h["anomaly_kinds"]:
        print("  --- anomaly classes (need a human) ---")
        for a in h["anomaly_kinds"]:
            print(f"    {a['anomaly_kind']:<26} {a['n']}")
    else:
        print("  no irreducible anomalies — every failure recovered or is a clean 'failed' retry")
    return 0


if __name__ == "__main__":
    sys.exit(main())
