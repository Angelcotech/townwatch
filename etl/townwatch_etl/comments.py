"""
Public comment intake + AI pre-moderation + triage.

A constrained public comment — the online equivalent of signing up to speak at
a meeting, NOT a forum. One structured submission per (user, agenda item), with
a Support/Oppose/Neutral stance, gated by:
  - identity      : a known, active app_user (Clerk-mirrored)
  - standing      : the item's body must be in the user's home city or its county
  - window        : the meeting must be upcoming (comment period still open)
  - one-per-item  : a user may comment once on a given item

Submitted comments land 'pending' and are then AI-moderated (cheap Haiku pass):
allowed → 'published' (counted in the tally, shown), refused → 'rejected' (kept
for audit, never shown). If the jurisdiction's fund can't afford the (tiny)
moderation spend, the comment stays 'pending' for manual review — a citizen is
never penalized for the town's funding state. Moderation spend is metered as
ESSENTIAL (draws to the hard floor, protected by the operating reserve).

The web layer reads published comments + tallies directly from Postgres; this
module owns only the writes (submit + triage), like corrections.py.

Triage:
    python -m townwatch_etl.comments --list-pending
    python -m townwatch_etl.comments --publish 12
    python -m townwatch_etl.comments --reject 13 --reason "off-topic"
    python -m townwatch_etl.comments --block-user 7
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .db import connect
from . import funds
from .config import ANTHROPIC_API_KEY
from .llm_client import record_anthropic

MODERATION_MODEL = "claude-haiku-4-5"
_STANCES = {"support", "oppose", "neutral"}
_MAX_BODY = 4000


# =====================================================================
# Submit
# =====================================================================

def _allowed_jurisdictions(conn, home_jurisdiction_id: int) -> set[int]:
    """The set a user with this home jurisdiction may comment on: the home
    place itself + its parent county (county fips_code = state_fips||county_fips)."""
    # county_fips is the full 5-digit county FIPS (state+county), which is exactly
    # a county jurisdiction's fips_code — so the parent county is a direct match.
    rows = conn.execute(
        """
        WITH home AS (
            SELECT id, county_fips FROM jurisdiction WHERE id = %s
        )
        SELECT j.id
        FROM jurisdiction j, home h
        WHERE j.id = h.id
           OR (j.jurisdiction_type = 'county'
               AND h.county_fips IS NOT NULL
               AND j.fips_code = h.county_fips)
        """,
        (home_jurisdiction_id,),
    ).fetchall()
    return {r["id"] for r in rows}


def _item_context(conn, agenda_item_id: int) -> dict[str, Any] | None:
    return conn.execute(
        """
        SELECT ai.id AS agenda_item_id, ai.title, ai.description,
               m.id  AS meeting_id, m.meeting_date,
               gb.jurisdiction_id,
               (m.meeting_date >= CURRENT_DATE) AS window_open
        FROM agenda_item ai
        JOIN meeting m        ON m.id = ai.meeting_id
        JOIN governing_body gb ON gb.id = m.governing_body_id
        WHERE ai.id = %s
        """,
        (agenda_item_id,),
    ).fetchone()


def submit(*, clerk_user_id: str, agenda_item_id: int, stance: str, body: str,
           ip: str | None = None) -> dict[str, Any]:
    """Record + moderate one public comment. Returns {id, status, deduped}."""
    stance = (stance or "").strip().lower()
    body = (body or "").strip()
    if stance not in _STANCES:
        raise ValueError("stance must be support, oppose, or neutral")
    if not body:
        raise ValueError("comment body is required")
    if not isinstance(agenda_item_id, int) or agenda_item_id <= 0:
        raise ValueError("agenda_item_id must be a positive integer")
    body = body[:_MAX_BODY]

    with connect() as conn:
        user = conn.execute(
            "SELECT id, status, home_jurisdiction_id FROM app_user WHERE clerk_user_id = %s",
            ((clerk_user_id or "").strip(),),
        ).fetchone()
        if user is None:
            raise ValueError("unknown user — sign in first")
        if user["status"] != "active":
            raise ValueError("this account can't post comments")
        if user["home_jurisdiction_id"] is None:
            raise ValueError("set your home jurisdiction before commenting")

        ctx = _item_context(conn, agenda_item_id)
        if ctx is None:
            raise ValueError("unknown agenda item")
        if not ctx["window_open"]:
            raise ValueError("the comment period for this meeting has closed")

        allowed = _allowed_jurisdictions(conn, user["home_jurisdiction_id"])
        if ctx["jurisdiction_id"] not in allowed:
            raise ValueError("commenting is open to residents of this city or county")

        # One comment per person per item — return the existing one on a repeat.
        existing = conn.execute(
            "SELECT id, status FROM public_comment "
            "WHERE app_user_id = %s AND agenda_item_id = %s",
            (user["id"], agenda_item_id),
        ).fetchone()
        if existing is not None:
            return {"id": existing["id"], "status": existing["status"], "deduped": True}

        row = conn.execute(
            """
            INSERT INTO public_comment
                (app_user_id, agenda_item_id, meeting_id, stance, body, reporter_ip)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (user["id"], agenda_item_id, ctx["meeting_id"], stance, body, ip),
        ).fetchone()
        comment_id = row["id"]
        jurisdiction_id = ctx["jurisdiction_id"]
        item_title = ctx["title"]
        item_desc = ctx["description"]

    # Moderate AFTER the insert is committed, so a moderation hiccup never loses
    # the comment (it just stays 'pending' for manual triage).
    new_status, reason = _moderate(item_title, item_desc, body, jurisdiction_id, comment_id)
    _apply_moderation(comment_id, new_status, reason)
    return {"id": comment_id, "status": new_status, "deduped": False}


# =====================================================================
# Moderation
# =====================================================================

_MOD_PROMPT = """You are moderating a written public comment submitted on a \
specific local-government agenda item, before it is published to the public \
record. Publish good-faith civic input even when it strongly supports or opposes \
the item or criticizes officials' decisions. REFUSE only if the comment contains \
harassment or personal attacks on private individuals, threats, hate speech, \
slurs, sexual content, doxxing (posting someone's private contact/address), \
obvious spam/advertising, or is gibberish/entirely unrelated to this item.

Agenda item title: {title}
Agenda item detail: {description}

Comment:
\"\"\"{body}\"\"\"

Respond with ONLY a JSON object: {{"allow": true|false, "reason": "<short reason>"}}"""


def _classify(title: str | None, description: str | None, body: str) -> tuple[bool, str]:
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = _MOD_PROMPT.format(
        title=(title or "(untitled)"),
        description=(description or "(no detail published)")[:1500],
        body=body,
    )
    resp = client.messages.create(
        model=MODERATION_MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    record_anthropic(MODERATION_MODEL, resp.usage)
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    start, end = text.find("{"), text.rfind("}")
    data = json.loads(text[start:end + 1]) if start >= 0 and end > start else {}
    return bool(data.get("allow")), str(data.get("reason", ""))[:300]


def _moderate(title, description, body, jurisdiction_id, comment_id) -> tuple[str, str]:
    """Return (new_status, reason): 'published' | 'rejected' | 'pending'."""
    if not ANTHROPIC_API_KEY:
        return "pending", "moderation unavailable — awaiting manual review"
    with funds.gate(jurisdiction_id, job_name="moderate_comment", ref_kind="comment",
                    ref_id=str(comment_id), description="comment moderation",
                    essential=True) as g:
        if g.paused:
            return "pending", "held — jurisdiction funding paused; awaiting manual review"
        try:
            allow, reason = _classify(title, description, body)
        except Exception as e:  # never lose the comment to a moderation error
            return "pending", f"moderation error ({type(e).__name__}) — awaiting manual review"
    return ("published", reason or "ok") if allow else ("rejected", reason or "refused")


def _apply_moderation(comment_id: int, new_status: str, reason: str) -> None:
    with connect() as conn:
        if new_status == "pending":
            conn.execute(
                "UPDATE public_comment SET moderation_reason = %s "
                "WHERE id = %s AND status = 'pending'",
                (reason, comment_id),
            )
        else:
            conn.execute(
                "UPDATE public_comment SET status = %s, moderation_reason = %s, "
                "moderated_at = now(), "
                "published_at = CASE WHEN %s = 'published' THEN now() ELSE NULL END "
                "WHERE id = %s",
                (new_status, reason, new_status, comment_id),
            )


# =====================================================================
# Triage
# =====================================================================

def list_pending(limit: int = 100) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT pc.id, pc.stance, pc.body, pc.moderation_reason, pc.created_at,
                   ai.title AS item_title, u.display_name, u.email
            FROM public_comment pc
            JOIN agenda_item ai ON ai.id = pc.agenda_item_id
            JOIN app_user u     ON u.id  = pc.app_user_id
            WHERE pc.status = 'pending'
            ORDER BY pc.created_at ASC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def publish(comment_id: int) -> bool:
    with connect() as conn:
        row = conn.execute(
            "UPDATE public_comment SET status = 'published', moderated_at = now(), "
            "published_at = now(), moderation_reason = COALESCE(moderation_reason, 'approved by operator') "
            "WHERE id = %s RETURNING id",
            (comment_id,),
        ).fetchone()
        return row is not None


def reject(comment_id: int, reason: str | None = None) -> bool:
    with connect() as conn:
        row = conn.execute(
            "UPDATE public_comment SET status = 'rejected', moderated_at = now(), "
            "published_at = NULL, moderation_reason = %s WHERE id = %s RETURNING id",
            (reason or "rejected by operator", comment_id),
        ).fetchone()
        return row is not None


def block_user(app_user_id: int) -> bool:
    """Block an account (no future comments) and hide its published comments."""
    with connect() as conn:
        u = conn.execute(
            "UPDATE app_user SET status = 'blocked', updated_at = now() "
            "WHERE id = %s RETURNING id",
            (app_user_id,),
        ).fetchone()
        if u is None:
            return False
        conn.execute(
            "UPDATE public_comment SET status = 'rejected', "
            "moderation_reason = 'author blocked', moderated_at = now(), published_at = NULL "
            "WHERE app_user_id = %s AND status = 'published'",
            (app_user_id,),
        )
        return True


def main() -> int:
    p = argparse.ArgumentParser(description="Triage public comments")
    p.add_argument("--list-pending", action="store_true")
    p.add_argument("--publish", type=int, metavar="ID")
    p.add_argument("--reject", type=int, metavar="ID")
    p.add_argument("--reason", default=None)
    p.add_argument("--block-user", type=int, metavar="APP_USER_ID")
    args = p.parse_args()

    if args.list_pending:
        for r in list_pending():
            who = r["display_name"] or r["email"] or "anon"
            print(f"#{r['id']:>4}  [{r['stance']}] {who} on \"{r['item_title']}\"  {r['created_at']:%Y-%m-%d}")
            print(f"        {r['body'][:200]}")
            if r["moderation_reason"]:
                print(f"        ({r['moderation_reason']})")
        return 0
    if args.publish is not None:
        print("published" if publish(args.publish) else "not found")
        return 0
    if args.reject is not None:
        print("rejected" if reject(args.reject, args.reason) else "not found")
        return 0
    if args.block_user is not None:
        print("blocked" if block_user(args.block_user) else "not found")
        return 0
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
