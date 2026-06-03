"""
User-account mirror — Clerk identity reflected into our own Postgres.

Auth lives in Clerk (the front door); this keeps the relationship + standing in
our DB so the data is ours and the web layer stays read-only. The web never
writes here directly — it forwards Clerk webhook events and the onboarding
home-jurisdiction choice to the ETL intake service, which calls these functions.

A user must have a row here (with a home jurisdiction set) before they can post
public comment.
"""

from __future__ import annotations

from typing import Any

from .db import connect


def upsert_user(*, clerk_user_id: str, email: str | None = None,
                display_name: str | None = None) -> int:
    """Insert or update the mirror of a Clerk user. Idempotent on clerk_user_id.
    COALESCE keeps an existing email/display_name when the event omits it."""
    clerk_user_id = (clerk_user_id or "").strip()
    if not clerk_user_id:
        raise ValueError("clerk_user_id is required")
    with connect() as conn:
        row = conn.execute(
            """
            INSERT INTO app_user (clerk_user_id, email, display_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (clerk_user_id) DO UPDATE SET
                email        = COALESCE(EXCLUDED.email, app_user.email),
                display_name = COALESCE(EXCLUDED.display_name, app_user.display_name),
                updated_at   = now()
            RETURNING id
            """,
            (clerk_user_id, (email or None), (display_name or None)),
        ).fetchone()
        return row["id"]


# A user may change their home jurisdiction at most once per this many days
# (the first choice is free). Stops jurisdiction-hopping to comment everywhere.
HOME_CHANGE_COOLDOWN_DAYS = 30


def set_home_jurisdiction(*, clerk_user_id: str, jurisdiction_id: int) -> int:
    """Set/change the user's self-declared home jurisdiction (standing anchor).
    First choice is free; subsequent changes are rate-limited to once per
    HOME_CHANGE_COOLDOWN_DAYS. Upserts the user so onboarding works even before
    the Clerk webhook lands. Setting the SAME jurisdiction is a no-op (not a
    change, so it never trips the cooldown)."""
    clerk_user_id = (clerk_user_id or "").strip()
    if not clerk_user_id:
        raise ValueError("clerk_user_id is required")
    if not isinstance(jurisdiction_id, int) or jurisdiction_id <= 0:
        raise ValueError("jurisdiction_id must be a positive integer")
    with connect() as conn:
        # Validate the jurisdiction exists (a real place, not a directory stub).
        j = conn.execute(
            "SELECT 1 FROM jurisdiction WHERE id = %s", (jurisdiction_id,)
        ).fetchone()
        if j is None:
            raise ValueError("unknown jurisdiction")
        # Ensure the user row exists.
        conn.execute(
            "INSERT INTO app_user (clerk_user_id) VALUES (%s) "
            "ON CONFLICT (clerk_user_id) DO NOTHING",
            (clerk_user_id,),
        )
        cur = conn.execute(
            "SELECT id, home_jurisdiction_id, "
            "  (home_jurisdiction_set_at IS NULL "
            f"   OR now() - home_jurisdiction_set_at >= interval '{HOME_CHANGE_COOLDOWN_DAYS} days') AS allowed, "
            f"  to_char(home_jurisdiction_set_at + interval '{HOME_CHANGE_COOLDOWN_DAYS} days', 'FMMon FMDD, YYYY') AS next_change "
            "FROM app_user WHERE clerk_user_id = %s",
            (clerk_user_id,),
        ).fetchone()
        # No-op when re-declaring the same home — never trips the cooldown.
        if cur["home_jurisdiction_id"] == jurisdiction_id:
            return cur["id"]
        # Changing to a DIFFERENT jurisdiction: enforce the cooldown.
        if not cur["allowed"]:
            raise ValueError(
                f"You can change your home town again on {cur['next_change']}."
            )
        conn.execute(
            "UPDATE app_user SET home_jurisdiction_id = %s, "
            "home_jurisdiction_set_at = now(), updated_at = now() "
            "WHERE id = %s",
            (jurisdiction_id, cur["id"]),
        )
        return cur["id"]


def get_user(conn, clerk_user_id: str) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT id, clerk_user_id, email, display_name, home_jurisdiction_id, status "
        "FROM app_user WHERE clerk_user_id = %s",
        (clerk_user_id,),
    ).fetchone()
