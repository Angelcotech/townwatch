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


def set_home_jurisdiction(*, clerk_user_id: str, jurisdiction_id: int) -> int:
    """Set the user's self-declared home jurisdiction (the standing anchor).
    Upserts the user first so onboarding works even if the webhook hasn't landed."""
    clerk_user_id = (clerk_user_id or "").strip()
    if not clerk_user_id:
        raise ValueError("clerk_user_id is required")
    if not isinstance(jurisdiction_id, int) or jurisdiction_id <= 0:
        raise ValueError("jurisdiction_id must be a positive integer")
    with connect() as conn:
        # Validate the jurisdiction exists (and is a real place, not a directory stub).
        j = conn.execute(
            "SELECT 1 FROM jurisdiction WHERE id = %s", (jurisdiction_id,)
        ).fetchone()
        if j is None:
            raise ValueError("unknown jurisdiction")
        # Ensure the user row exists, then set standing.
        conn.execute(
            "INSERT INTO app_user (clerk_user_id) VALUES (%s) "
            "ON CONFLICT (clerk_user_id) DO NOTHING",
            (clerk_user_id,),
        )
        row = conn.execute(
            "UPDATE app_user SET home_jurisdiction_id = %s, updated_at = now() "
            "WHERE clerk_user_id = %s RETURNING id",
            (jurisdiction_id, clerk_user_id),
        ).fetchone()
        return row["id"]


def get_user(conn, clerk_user_id: str) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT id, clerk_user_id, email, display_name, home_jurisdiction_id, status "
        "FROM app_user WHERE clerk_user_id = %s",
        (clerk_user_id,),
    ).fetchone()
