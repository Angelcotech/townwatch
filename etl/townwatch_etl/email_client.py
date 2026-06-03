"""
Outbound email — the one place TownWatch sends mail.

Used to deliver the compiled public-comment digest to a meeting's records
custodian. Thin wrapper over Resend's HTTP API (no SMTP creds to manage). If
RESEND_API_KEY is unset it's a safe no-op that logs what it WOULD send — so dev
and unconfigured deploys never blast real email.

Env:
    RESEND_API_KEY   — Resend API key (unset → no-op).
    RESEND_FROM      — From header, e.g. "TownWatch <forum@townwatch.org>".
                       Must be a verified Resend domain in production.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

_RESEND_URL = "https://api.resend.com/emails"


def send_email(*, to: str, subject: str, text: str,
               reply_to: str | None = None) -> dict[str, Any]:
    """Send a plain-text email. Returns {sent, ...}. Never raises on a missing
    key (no-op); raises only on a genuine API error so the caller can record a
    'failed' status."""
    to = (to or "").strip()
    if not to:
        return {"sent": False, "reason": "no recipient"}

    api_key = os.environ.get("RESEND_API_KEY")
    sender = os.environ.get("RESEND_FROM", "TownWatch <forum@townwatch.org>")
    if not api_key:
        print(f"  ⊘ RESEND_API_KEY unset — not sending. Would email {to}: {subject!r}")
        return {"sent": False, "reason": "no api key"}

    payload: dict[str, Any] = {"from": sender, "to": [to], "subject": subject, "text": text}
    if reply_to:
        payload["reply_to"] = reply_to

    r = httpx.post(
        _RESEND_URL,
        headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
        json=payload,
        timeout=30.0,
    )
    r.raise_for_status()
    return {"sent": True, "id": r.json().get("id")}
