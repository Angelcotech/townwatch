"""
Backfill per-document AI summaries for meetings already extracted before
the `document_summary` field existed in the Pydantic schemas.

Reads existing structured data (agenda_item rows for the agenda summary,
motion rows for the minutes summary), sends a compact JSON of those rows
to Haiku, writes the result back to `meeting.agenda_ai_summary` /
`meeting.minutes_ai_summary`.

Idempotent: skips meetings that already have a summary in the target column.

Run:
    python -m townwatch_etl.jobs.backfill_summaries                # both kinds
    python -m townwatch_etl.jobs.backfill_summaries --kind agenda  # only agenda
    python -m townwatch_etl.jobs.backfill_summaries --kind minutes # only minutes
    python -m townwatch_etl.jobs.backfill_summaries --limit 5      # first 5
    python -m townwatch_etl.jobs.backfill_summaries --jurisdiction grovetown-ga
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Literal

import anthropic

from ..config import ANTHROPIC_API_KEY
from ..db import connect
from .. import funds
from ..llm_client import record_anthropic


SUMMARY_MODEL = "claude-haiku-4-5"
MAX_TOKENS = 400


AGENDA_PROMPT = """\
You are writing a 2-3 sentence plain-English summary of a meeting agenda for
a citizen who has not opened the PDF. The structured docket below was already
extracted from the agenda by another system.

Write the summary so that:
- It leads with the substantive items on the docket (rezoning, variance,
  contracts, budget, presentations), not procedural boilerplate.
- It names applicants, locations, and dollar amounts when notable.
- It flags returning items (continued, second readings) when present.
- It does NOT speculate about outcomes. The agenda is a list of items
  BEFORE the meeting happened.

Return only the summary text — no preface, no quotes, no markdown. 2-3
sentences total.

Meeting: {body_name} on {meeting_date}
Docket:
{docket_json}
"""


MINUTES_PROMPT = """\
You are writing a 2-3 sentence plain-English summary of meeting minutes
for a citizen who has not opened the PDF. The structured motions and
votes below were already extracted by another system.

Write the summary so that:
- It leads with the most consequential outcomes (what passed, failed,
  tabled, withdrew).
- It names dollar amounts, applicants, and recusals when notable.
- It quantifies vote splits when they were close or unusual.
- It does NOT pad with procedural details (roll call, minutes approval)
  unless something unusual happened.

Return only the summary text — no preface, no quotes, no markdown. 2-3
sentences total.

Meeting: {body_name} on {meeting_date}
Motions:
{motions_json}
"""


def _summarize(client: anthropic.Anthropic, prompt: str) -> str:
    """Single Haiku call. Returns trimmed summary text."""
    resp = client.messages.create(
        model=SUMMARY_MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    record_anthropic(SUMMARY_MODEL, resp.usage)
    text = ""
    for block in resp.content:
        if getattr(block, "type", "") == "text":
            text = block.text
            break
    return text.strip().strip('"').strip("'").strip()


def _agenda_docket_json(conn, meeting_id: int) -> str:
    rows = conn.execute(
        """
        SELECT item_number, title, item_type, applicant_name,
               recommended_action, hearing_status, locations
        FROM agenda_item
        WHERE meeting_id = %s
        ORDER BY source_page NULLS LAST, id
        """,
        (meeting_id,),
    ).fetchall()
    docket = [
        {
            k: v for k, v in dict(r).items()
            if v not in (None, [], {}, "")
        }
        for r in rows
    ]
    return json.dumps(docket, indent=2, default=str)


def _minutes_motions_json(conn, meeting_id: int) -> str:
    rows = conn.execute(
        """
        SELECT motion_number, title, motion_type, outcome,
               vote_tally_yes, vote_tally_no, vote_tally_abstain,
               petitioner_name, staff_recommender, movant, seconder,
               dollar_amount, locations, description
        FROM motion
        WHERE meeting_id = %s
        ORDER BY id
        """,
        (meeting_id,),
    ).fetchall()
    motions = []
    for r in rows:
        d = {k: v for k, v in dict(r).items() if v not in (None, [], {}, "")}
        # Trim description so we don't blow the prompt budget for huge motions
        if "description" in d and isinstance(d["description"], str):
            d["description"] = d["description"][:500]
        motions.append(d)
    return json.dumps(motions, indent=2, default=str)


def _list_pending(
    conn,
    kind: Literal["agenda", "minutes"],
    jurisdiction_slug: str | None,
    limit: int | None,
) -> list[dict]:
    """Meetings that have extracted data but no summary in the target column."""
    if kind == "agenda":
        sql = """
            SELECT m.id, m.meeting_date, gb.name AS body_name,
                   j.display_name AS jurisdiction, j.id AS jurisdiction_id
            FROM meeting m
            JOIN governing_body gb ON gb.id = m.governing_body_id
            JOIN jurisdiction j ON j.id = gb.jurisdiction_id
            WHERE m.agenda_ai_summary IS NULL
              AND EXISTS (SELECT 1 FROM agenda_item ai WHERE ai.meeting_id = m.id)
        """
    else:
        sql = """
            SELECT m.id, m.meeting_date, gb.name AS body_name,
                   j.display_name AS jurisdiction, j.id AS jurisdiction_id
            FROM meeting m
            JOIN governing_body gb ON gb.id = m.governing_body_id
            JOIN jurisdiction j ON j.id = gb.jurisdiction_id
            WHERE m.minutes_ai_summary IS NULL
              AND EXISTS (SELECT 1 FROM motion mo WHERE mo.meeting_id = m.id)
        """
    params: list = []
    if jurisdiction_slug:
        from ..jurisdiction import load_config, jurisdiction_fips
        cfg = load_config(jurisdiction_slug)
        sql += " AND j.fips_code = %s"
        params.append(jurisdiction_fips(cfg))
    sql += " ORDER BY m.meeting_date DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def run_kind(
    kind: Literal["agenda", "minutes"],
    jurisdiction_slug: str | None,
    limit: int | None,
) -> tuple[int, int]:
    """Returns (succeeded, errored) counts."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    with connect() as conn:
        pending = _list_pending(conn, kind, jurisdiction_slug, limit)
        print(f"[{kind}] {len(pending)} meeting(s) need a summary")
        succeeded = 0
        errored = 0
        paused_jids: set[int] = set()
        for m in pending:
            jid = m["jurisdiction_id"]
            if jid in paused_jids:
                continue
            # Per-jurisdiction spend gate: this is a paid Haiku call, so it must
            # reserve/settle against the fund like the other extractors — else a
            # funded jurisdiction's balance under-counts its true spend.
            with funds.gate(jid, meeting_id=m["id"], job_name="backfill_summaries",
                            ref_kind="meeting", ref_id=str(m["id"]),
                            description=f"summary:{kind}") as g:
                if g.paused:
                    paused_jids.add(jid)
                    print(f"  ⏸ meeting {m['id']}: jurisdiction paused (insufficient funds) — skipping")
                    continue
                try:
                    if kind == "agenda":
                        docket = _agenda_docket_json(conn, m["id"])
                        prompt = AGENDA_PROMPT.format(
                            body_name=m["body_name"],
                            meeting_date=m["meeting_date"],
                            docket_json=docket,
                        )
                    else:
                        motions = _minutes_motions_json(conn, m["id"])
                        prompt = MINUTES_PROMPT.format(
                            body_name=m["body_name"],
                            meeting_date=m["meeting_date"],
                            motions_json=motions,
                        )
                    summary = _summarize(client, prompt)
                    if not summary:
                        print(f"  ✗ meeting {m['id']}: empty summary returned")
                        errored += 1
                        continue
                    column = "agenda_ai_summary" if kind == "agenda" else "minutes_ai_summary"
                    conn.execute(
                        f"UPDATE meeting SET {column} = %s, updated_at = now() WHERE id = %s",
                        (summary, m["id"]),
                    )
                    succeeded += 1
                    print(f"  ✓ meeting {m['id']} ({m['body_name']} {m['meeting_date']}): {summary[:100]}{'...' if len(summary) > 100 else ''}")
                except Exception as e:
                    errored += 1
                    print(f"  ✗ meeting {m['id']}: {e}")
        return succeeded, errored


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=["agenda", "minutes", "both"], default="both")
    parser.add_argument("--jurisdiction", help="Restrict to this slug")
    parser.add_argument("--limit", type=int, help="Max meetings to process per kind")
    args = parser.parse_args()

    totals = {"succeeded": 0, "errored": 0}
    kinds = ("agenda", "minutes") if args.kind == "both" else (args.kind,)
    for k in kinds:
        s, e = run_kind(k, args.jurisdiction, args.limit)
        totals["succeeded"] += s
        totals["errored"] += e
    print(f"\nTotal: {totals['succeeded']} succeeded, {totals['errored']} errored")
    return 0 if totals["errored"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
