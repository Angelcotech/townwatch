"""
Monitor the records-custodian (clerk) email — the one address records requests and
public-comment digests are delivered to. If it's malformed or its domain is dead,
everything we send silently never arrives. This stamps each jurisdiction's contact
health so a broken clerk email SURFACES instead of failing in the dark.

Two cheap, false-alarm-free signals (no model spend, no new dependency):
  * validity   — syntax + the domain actually resolves (stdlib socket). Catches
                 typos and dead/abandoned domains.
  * delivery   — a real send that fails marks the contact 'undeliverable'
                 (recorded by submit_comments, the harder signal); a successful
                 send clears it back to 'verified'.

Status precedence is worst-wins: a real delivery failure ('undeliverable') is not
cleared by mere syntactic validity — only a later successful send clears it.

NOT done here (deliberately, to avoid false alarms): re-extracting the published
clerk email and guessing it 'changed'. Without a configured contact-source page
that's unreliable and would itself erode trust. Tracked as a follow-on (needs a
contact-source URL or a vision pass).

Idempotent. A WORSENING transition (verified/unchecked -> unverified) emits one
activity event + one admin flag; steady state is silent (no spam).

Run:
    python -m townwatch_etl.jobs.monitor_clerk_contact
    python -m townwatch_etl.jobs.monitor_clerk_contact --jurisdiction grovetown-ga
"""

from __future__ import annotations

import argparse
import re
import socket
import sys

from ..db import connect

# Pragmatic email shape: something@domain.tld (not full RFC 5322, deliberately).
_EMAIL_RE = re.compile(r"^[^@\s]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})$")

# Ranked so we can tell a "worsening" transition from an improving one.
_RANK = {"verified": 0, None: 1, "unverified": 2, "changed": 3, "undeliverable": 4}


def check_validity(email: str) -> tuple[str, str]:
    """(status, reason). 'verified' if it parses and its domain resolves;
    'unverified' otherwise. Domain resolution (A/AAAA via getaddrinfo) is a cheap
    dependency-free proxy for deliverability — it catches dead domains."""
    e = (email or "").strip()
    m = _EMAIL_RE.match(e)
    if not m:
        return "unverified", "malformed address"
    domain = m.group(1)
    try:
        socket.getaddrinfo(domain, None)
    except socket.gaierror:
        return "unverified", f"domain does not resolve ({domain})"
    except Exception as exc:  # network hiccup — inconclusive, don't downgrade
        return "", f"lookup inconclusive ({type(exc).__name__})"
    return "verified", "ok"


def _apply(conn, jid: int, name: str, new_status: str, reason: str, prev: str | None) -> str | None:
    """Worst-wins merge + persist + flag a worsening transition. Returns the
    status actually written (or None on an inconclusive no-op)."""
    if not new_status:
        return None  # inconclusive lookup — leave prior status untouched
    # A real delivery failure outranks syntactic validity; don't let a 'verified'
    # from this monitor paper over a known bounce.
    if prev == "undeliverable" and new_status == "verified":
        new_status = "undeliverable"

    conn.execute(
        "UPDATE jurisdiction SET records_custodian_email_status = %s, "
        "records_custodian_email_checked_at = now() WHERE id = %s",
        (new_status, jid),
    )
    # Surface a worsening change to the OPERATOR (admin queue) only — clerk-contact
    # health is internal ops telemetry, not a citizen-facing milestone, so it never
    # touches the public activity feed. Steady state is silent (no spam).
    if _RANK.get(new_status, 1) > _RANK.get(prev, 1) and new_status != "verified":
        conn.execute(
            "INSERT INTO pipeline_failure (job_name, step, message, context) "
            "VALUES ('monitor_clerk_contact', %s, %s, %s::jsonb)",
            (new_status, f"{name}: clerk email {new_status} — {reason}",
             '{"jurisdiction_id": %d}' % jid),
        )
    return new_status


def run(jurisdiction_slug: str | None) -> int:
    where = ""
    params: list = []
    if jurisdiction_slug:
        where = "WHERE LOWER(REPLACE(name, ' ', '-') || '-' || LOWER(state_abbr)) = LOWER(%s)"
        params.append(jurisdiction_slug)

    with connect() as conn:
        rows = conn.execute(
            f"SELECT id, display_name, records_custodian_email, "
            f"records_custodian_email_status AS prev "
            f"FROM jurisdiction {where} ORDER BY id",
            params,
        ).fetchall()
        checked = flagged = 0
        for r in rows:
            email = r["records_custodian_email"]
            if not email:
                # No clerk email on file at all — can't deliver anything to them.
                status = _apply(conn, r["id"], r["display_name"], "unverified",
                                "no clerk email on file", r["prev"])
                if status:
                    flagged += 1
                continue
            new_status, reason = check_validity(email)
            checked += 1
            status = _apply(conn, r["id"], r["display_name"], new_status, reason, r["prev"])
            if status and status != "verified":
                flagged += 1
                print(f"  ⚠ {r['display_name']}: {status} — {reason} ({email})")
    print(f"--- clerk-contact check: {checked} checked, {flagged} need review ---")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jurisdiction", help="slug like 'grovetown-ga'; default all")
    args = parser.parse_args()
    return run(args.jurisdiction)


if __name__ == "__main__":
    sys.exit(main())
