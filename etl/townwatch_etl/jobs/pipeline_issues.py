"""
pipeline_issues — triage the automation's operational issues.

The interface for resolving pipeline_issue rows (see pipeline_health.py / migration 050).
Built for an agent in the terminal (Claude Code) as much as a human: `list` is the
worklist, `show` gives everything needed to DIAGNOSE (the issue + its context + the linked
pipeline_failure tracebacks + the jurisdiction's recent run heartbeats), and `resolve`
is the "mark fixed" once the underlying cause is corrected.

    python -m townwatch_etl.jobs.pipeline_issues list [--jurisdiction grovetown-ga] [--status open]
    python -m townwatch_etl.jobs.pipeline_issues show 42
    python -m townwatch_etl.jobs.pipeline_issues resolve 42 --notes "fixed the parser" [--diagnosis "..."]
    python -m townwatch_etl.jobs.pipeline_issues resolve 42 --wont-fix --notes "external site dead"

Note: the observer (refresh_pipeline_health) auto-resolves an issue once its condition
clears, so resolving here is for problems you fixed in CODE/CONFIG that won't self-clear.
"""

from __future__ import annotations

import argparse
import sys

from ..db import connect
from .. import pipeline_health


def _jid(slug: str) -> int | None:
    from ..jurisdiction import load_config, jurisdiction_fips
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM jurisdiction WHERE fips_code = %s",
            (jurisdiction_fips(load_config(slug)),),
        ).fetchone()
        return row["id"] if row else None


def _cmd_list(args) -> int:
    jid = _jid(args.jurisdiction) if args.jurisdiction else None
    status = None if args.status == "all" else args.status
    with connect() as conn:
        issues = pipeline_health.list_issues(conn, jurisdiction_id=jid, status=status, limit=args.limit)
    if not issues:
        print("No issues.")
        return 0
    print(f"{'ID':>5}  {'SEV':<6} {'STATUS':<8} {'JURISDICTION':<28} TITLE")
    for i in issues:
        print(f"{i['id']:>5}  {i['severity']:<6} {i['status']:<8} "
              f"{(i['jurisdiction'] or '')[:27]:<28} {i['title']}")
    print(f"\n{len(issues)} issue(s). `show <id>` for detail.")
    return 0


def _cmd_show(args) -> int:
    with connect() as conn:
        i = pipeline_health.get_issue(conn, args.id)
        if i is None:
            print(f"No issue {args.id}.")
            return 1
        print(f"#{i['id']}  [{i['severity']}] {i['status']}  — {i['jurisdiction']} ({i['state_abbr']})")
        print(f"  type:   {i['issue_type']}  (dedupe_key={i['dedupe_key']})")
        print(f"  title:  {i['title']}")
        print(f"  seen:   {i['first_observed_at']} → {i['last_observed_at']}")
        if i["resolved_at"]:
            print(f"  resolved: {i['resolved_at']} by {i['resolved_by']}")
            if i["diagnosis"]:
                print(f"  diagnosis: {i['diagnosis']}")
            if i["fix_notes"]:
                print(f"  fix:    {i['fix_notes']}")
        print(f"\n  {i['detail'] or ''}")
        ctx = i["context"] or {}
        if ctx:
            print(f"\n  context: {ctx}")

        # Prior fixes for this problem CLASS — read these BEFORE diagnosing; a fix
        # applied to one jurisdiction shows here for the same problem elsewhere.
        prior = pipeline_health.fixes_for(conn, i["dedupe_key"])
        if prior:
            print("\n  --- prior fixes for this problem (dedupe_key) ---")
            for fx in prior:
                print(f"  {fx['created_at']:%Y-%m-%d} [{fx['resolution']}] "
                      f"{fx['jurisdiction'] or 'org'} by {fx['resolved_by']}")
                if fx["diagnosis"]:
                    print(f"      cause: {fx['diagnosis']}")
                if fx["fix_notes"]:
                    print(f"      fix:   {fx['fix_notes']}")

        # Linked failure tracebacks — the diagnostic payload.
        fids = (ctx or {}).get("failure_ids") or []
        if fids:
            print("\n  --- linked pipeline_failure rows ---")
            for f in conn.execute(
                "SELECT id, step, exception_class, message, traceback, context, created_at "
                "FROM pipeline_failure WHERE id = ANY(%s) ORDER BY created_at DESC",
                (list(fids),),
            ).fetchall():
                print(f"\n  [{f['id']}] {f['created_at']:%Y-%m-%d %H:%M}  "
                      f"{f['exception_class'] or ''} {f['step'] or ''}")
                print(f"      {f['message']}")
                # The failure's own context is often the REAL evidence — for
                # recovery anomalies the per-strategy attempt trail says exactly
                # what error every ladder stage hit (e.g. an account-level API
                # error masquerading as unknown_extraction_error), and the
                # document URL identifies the failing source. Render it instead
                # of making the diagnoser go query the DB by hand.
                fctx = f["context"] or {}
                if isinstance(fctx, str):
                    import json as _json
                    try:
                        fctx = _json.loads(fctx)
                    except ValueError:
                        fctx = {}
                for att in fctx.get("attempts") or []:
                    err = " ".join((att.get("error") or "").split())
                    print(f"        attempt {att.get('strategy')}: "
                          f"{att.get('error_type')}: {err[:220]}")
                for key, val in fctx.items():
                    if key in ("attempts", "anomaly_kind"):
                        continue
                    print(f"        {key}: {val}")
                if f["traceback"]:
                    tb = "\n      ".join(f["traceback"].strip().splitlines()[-8:])
                    print(f"      {tb}")

        # Recent run heartbeats for the jurisdiction — is the pipeline running at all?
        runs = pipeline_health.recent_runs(conn, i["jurisdiction_id"], limit=5)
        if runs:
            print("\n  --- recent runs ---")
            for r in runs:
                print(f"  {r['started_at']:%Y-%m-%d %H:%M}  {r['outcome']:<8} "
                      f"surfaced={r['surfaced']}  errors={r['error_count']}")
    return 0


def _cmd_resolve(args) -> int:
    status = "wont_fix" if args.wont_fix else "resolved"
    # The resolution IS the knowledge-base entry — require a breadcrumb so the next
    # session (human or agent) isn't troubleshooting this from scratch.
    if not (args.notes and args.notes.strip()):
        print("Refusing to resolve without --notes (what you changed, or why it's won't-fix).")
        return 2
    if status == "resolved" and not (args.diagnosis and args.diagnosis.strip()):
        print("Refusing to resolve without --diagnosis (the root cause). "
              "Use --wont-fix if there's nothing to fix.")
        return 2
    with connect() as conn:
        ok = pipeline_health.resolve_issue(
            conn, args.id, resolved_by="claude-code", status=status,
            notes=args.notes, diagnosis=args.diagnosis,
        )
    if ok:
        print(f"Issue {args.id} marked {status}.")
        return 0
    print(f"Issue {args.id} not open (already resolved, or no such id).")
    return 1


def _cmd_fixes(args) -> int:
    with connect() as conn:
        rows = pipeline_health.list_fixes(conn, grep=args.grep, limit=args.limit)
    if not rows:
        print("No fixes recorded yet." + (f" (grep={args.grep!r})" if args.grep else ""))
        return 0
    for f in rows:
        print(f"#{f['id']} {f['created_at']:%Y-%m-%d} [{f['resolution']}] {f['dedupe_key']} "
              f"— {f['jurisdiction'] or 'org'} by {f['resolved_by']}")
        if f["diagnosis"]:
            print(f"    cause: {f['diagnosis']}")
        if f["fix_notes"]:
            print(f"    fix:   {f['fix_notes']}")
    print(f"\n{len(rows)} fix(es). The pipeline-health knowledge base.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Triage pipeline-health issues.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list issues (default: open)")
    p_list.add_argument("--jurisdiction", help="restrict to one slug")
    p_list.add_argument("--status", default="open",
                        choices=["open", "resolved", "wont_fix", "all"])
    p_list.add_argument("--limit", type=int, default=200)
    p_list.set_defaults(fn=_cmd_list)

    p_show = sub.add_parser("show", help="full detail + tracebacks + recent runs")
    p_show.add_argument("id", type=int)
    p_show.set_defaults(fn=_cmd_show)

    p_res = sub.add_parser("resolve", help="mark an issue fixed (or wont-fix)")
    p_res.add_argument("id", type=int)
    p_res.add_argument("--notes", help="what was changed")
    p_res.add_argument("--diagnosis", help="root cause")
    p_res.add_argument("--wont-fix", action="store_true", help="suppress instead of fix")
    p_res.set_defaults(fn=_cmd_resolve)

    p_fix = sub.add_parser("fixes", help="the resolution knowledge base (prior fixes)")
    p_fix.add_argument("--grep", help="filter across dedupe_key / diagnosis / fix_notes")
    p_fix.add_argument("--limit", type=int, default=50)
    p_fix.set_defaults(fn=_cmd_fixes)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
