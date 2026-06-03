"""
Submit the live-forum public-comment digest to officials.

Closes the forum and delivers the record. For each meeting that has crossed its
"12 hours before the meeting" cutoff and has published comments not yet
submitted:

  1. Compile the published comments per agenda item (Support/Oppose/Neutral
     tally + each comment), into a plain-text digest.
  2. An AI AGENT reviews the COMPILED digest one last time before it goes out —
     the individual comments were already moderated at submission, so this is a
     final aggregate sanity check (nothing harassing/doxxing slipped through the
     compilation; it reads as on-topic civic comment). Fully automated send, but
     never un-reviewed.
  3. If cleared, email it to the jurisdiction's records custodian (the clerk who
     runs the meeting). Record the outcome in comment_digest and stamp
     meeting.comments_submitted_at so it's never double-sent.

The comment window itself closes on time (−12h), enforced in comments.submit;
this job is the delivery + record side.

Run (cron — hourly is plenty, the −12h window is wide):
    python -m townwatch_etl.jobs.submit_comments
    python -m townwatch_etl.jobs.submit_comments --dry-run    # compile + review, don't send/mark
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ..db import connect
from .. import funds
from .. import email_client
from ..config import ANTHROPIC_API_KEY
from ..llm_client import record_anthropic

REVIEW_MODEL = "claude-haiku-4-5"


def _candidates(conn) -> list[dict[str, Any]]:
    """Meetings past their −12h cutoff with published comments, not yet submitted."""
    return conn.execute(
        """
        SELECT m.id AS meeting_id, m.meeting_date, m.meeting_time,
               gb.name AS body_name, gb.jurisdiction_id,
               j.display_name AS jurisdiction, j.state_abbr,
               j.records_custodian_email, j.records_custodian_name
        FROM meeting m
        JOIN governing_body gb ON gb.id = m.governing_body_id
        JOIN jurisdiction j   ON j.id = gb.jurisdiction_id
        WHERE m.comments_submitted_at IS NULL
          AND EXISTS (SELECT 1 FROM public_comment pc
                      WHERE pc.meeting_id = m.id AND pc.status = 'published')
          -- 12 hours before the (naive local) meeting start has passed. Times are
          -- local wall-clock; the 12h buffer absorbs the tz imprecision.
          AND now() >= (m.meeting_date + COALESCE(m.meeting_time, time '18:00'))
                       AT TIME ZONE 'UTC' - interval '12 hours'
          -- Safety: don't dredge up long-past meetings if the job was down.
          AND m.meeting_date >= CURRENT_DATE - 2
        ORDER BY m.meeting_date
        """,
    ).fetchall()


def _compile(conn, meeting_id: int) -> tuple[str, int, int]:
    """Build the digest text. Returns (body, item_count, comment_count)."""
    rows = conn.execute(
        """
        SELECT ai.item_number, ai.title,
               pc.stance, pc.body, COALESCE(u.display_name, 'Resident') AS author
        FROM agenda_item ai
        JOIN public_comment pc ON pc.agenda_item_id = ai.id AND pc.status = 'published'
        JOIN app_user u        ON u.id = pc.app_user_id
        WHERE ai.meeting_id = %s
        ORDER BY ai.id, pc.published_at ASC NULLS LAST, pc.id
        """,
        (meeting_id,),
    ).fetchall()

    by_item: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for r in rows:
        key = f"{r['item_number'] or ''} {r['title']}".strip()
        it = by_item.get(key)
        if it is None:
            it = {"support": 0, "oppose": 0, "neutral": 0, "comments": []}
            by_item[key] = it
            order.append(key)
        if r["stance"] in ("support", "oppose", "neutral"):
            it[r["stance"]] += 1
        if (r["body"] or "").strip():
            it["comments"].append((r["stance"], r["author"], r["body"].strip()))

    lines: list[str] = []
    comment_count = 0
    for key in order:
        it = by_item[key]
        lines.append(f"== {key} ==")
        lines.append(f"Support: {it['support']}  |  Oppose: {it['oppose']}  |  Neutral: {it['neutral']}")
        for stance, author, body in it["comments"]:
            lines.append(f"  - [{stance.capitalize()}] {author}: {body}")
            comment_count += 1
        lines.append("")
    return "\n".join(lines).strip(), len(order), comment_count


def _agent_review(digest: str, body_name: str) -> tuple[bool, str]:
    """Final aggregate review of the compiled digest. (cleared, note)."""
    if not ANTHROPIC_API_KEY:
        return False, "review unavailable (no API key) — held for operator"
    import anthropic

    prompt = (
        "You are doing a FINAL review of a compiled public-comment digest before it is "
        f"emailed to the city/county clerk who runs the {body_name} meeting. The individual "
        "comments were already moderated when submitted, so this is an aggregate sanity check. "
        "CLEAR it for sending unless the compiled digest as a whole is inappropriate to send to "
        "a government official — e.g. harassment, threats, doxxing, or spam slipped through, or "
        "it is obviously not genuine civic comment. Strongly-worded support or opposition is fine.\n\n"
        f"Digest:\n\"\"\"\n{digest[:8000]}\n\"\"\"\n\n"
        'Respond with ONLY JSON: {"cleared": true|false, "note": "<short reason>"}'
    )
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=REVIEW_MODEL, max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    record_anthropic(REVIEW_MODEL, resp.usage)
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    s, e = text.find("{"), text.rfind("}")
    data = json.loads(text[s:e + 1]) if s >= 0 and e > s else {}
    return bool(data.get("cleared")), str(data.get("note", ""))[:300]


def _record(conn, *, meeting_id, recipient_email, recipient_name, item_count,
            comment_count, decision, note, body, status, sent_at) -> None:
    conn.execute(
        """
        INSERT INTO comment_digest
            (meeting_id, recipient_email, recipient_name, item_count, comment_count,
             agent_decision, agent_note, body, status, sent_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (meeting_id, recipient_email, recipient_name, item_count, comment_count,
         decision, note, body, status, sent_at),
    )
    conn.execute(
        "UPDATE meeting SET comments_submitted_at = now() WHERE id = %s",
        (meeting_id,),
    )


def _process(m: dict[str, Any], *, dry_run: bool) -> str:
    mid = m["meeting_id"]
    jid = m["jurisdiction_id"]
    with connect() as conn:
        digest, item_count, comment_count = _compile(conn, mid)
    if not digest:
        return "empty"

    # Agent review is paid (Haiku) — meter as essential per jurisdiction.
    with funds.gate(jid, job_name="submit_comments", ref_kind="digest",
                    ref_id=str(mid), description="comment digest review", essential=True) as g:
        if g.paused:
            print(f"  ⏸ meeting {mid}: funds paused — deferring digest")
            return "paused"
        cleared, note = _agent_review(digest, m["body_name"])

    when = f"{m['jurisdiction']} {m['body_name']} — {m['meeting_date']}"
    subject = f"Public comment for the {m['body_name']} meeting on {m['meeting_date']:%b %d, %Y}"
    intro = (
        f"The following public comments were submitted by verified residents through "
        f"TownWatch ahead of the {when} meeting, organized by agenda item. They are "
        f"provided for the record.\n\n"
    )
    outro = (
        "\n\n— Compiled by TownWatch, non-partisan civic infrastructure. "
        "This inbox is not monitored."
    )
    full = intro + digest + outro

    if dry_run:
        print(f"  [dry-run] meeting {mid}: items={item_count} comments={comment_count} "
              f"cleared={cleared} recipient={m['records_custodian_email'] or '(none)'}")
        return "dry-run"

    if not cleared:
        with connect() as conn:
            _record(conn, meeting_id=mid, recipient_email=m["records_custodian_email"],
                    recipient_name=m["records_custodian_name"], item_count=item_count,
                    comment_count=comment_count, decision="held", note=note,
                    body=full, status="held", sent_at=None)
        print(f"  ⚠ meeting {mid}: agent HELD the digest ({note}) — recorded for operator")
        return "held"

    recipient = (m["records_custodian_email"] or "").strip()
    if not recipient:
        with connect() as conn:
            _record(conn, meeting_id=mid, recipient_email=None,
                    recipient_name=m["records_custodian_name"], item_count=item_count,
                    comment_count=comment_count, decision="cleared", note=note,
                    body=full, status="no_recipient", sent_at=None)
        print(f"  ⊘ meeting {mid}: no custodian email on file — digest recorded, not sent")
        return "no_recipient"

    try:
        result = email_client.send_email(to=recipient, subject=subject, text=full)
        sent = result.get("sent", False)
    except Exception as e:
        with connect() as conn:
            _record(conn, meeting_id=mid, recipient_email=recipient,
                    recipient_name=m["records_custodian_name"], item_count=item_count,
                    comment_count=comment_count, decision="cleared", note=f"send error: {e}",
                    body=full, status="failed", sent_at=None)
        print(f"  ✗ meeting {mid}: send failed: {e}")
        return "failed"

    from datetime import datetime, timezone
    with connect() as conn:
        _record(conn, meeting_id=mid, recipient_email=recipient,
                recipient_name=m["records_custodian_name"], item_count=item_count,
                comment_count=comment_count, decision="cleared", note=note,
                body=full, status="sent" if sent else "no_recipient",
                sent_at=datetime.now(timezone.utc) if sent else None)
    print(f"  {'✓ sent' if sent else '⊘ not sent (no email service)'} meeting {mid}: "
          f"{comment_count} comments across {item_count} item(s) → {recipient}")
    return "sent" if sent else "skipped_no_service"


def main() -> int:
    p = argparse.ArgumentParser(description="Submit public-comment digests to officials")
    p.add_argument("--dry-run", action="store_true", help="compile + review, don't send or mark")
    args = p.parse_args()

    with connect() as conn:
        rows = [dict(r) for r in _candidates(conn)]
    print(f"Comment digests due: {len(rows)}")
    tally: dict[str, int] = {}
    for m in rows:
        outcome = _process(m, dry_run=args.dry_run)
        tally[outcome] = tally.get(outcome, 0) + 1
    print(f"Done. {tally}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
