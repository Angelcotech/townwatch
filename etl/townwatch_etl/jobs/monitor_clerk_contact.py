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
# Loose finder for emails embedded in page HTML (mailto: links, plain text).
_PAGE_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Local-part keywords that mark an address as a plausible clerk/records contact.
_CLERKISH = ("clerk", "record", "custodian", "openrecord", "open-record", "foia", "ora")

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


def check_drift(source_url: str, stored_email: str) -> tuple[str, str, str | None]:
    """Re-read the published clerk email from `source_url` and compare it to the one
    we hold. Returns (status, reason, observed_email).

    This is built to NEVER raise a false 'changed':
      * stored email STILL on the page      -> ('verified', positive confirmation, stored)
      * stored email GONE, but a DIFFERENT clerk/records-looking address on the SAME
        domain is published                 -> ('changed', reason, that address)
      * fetch fails / no email / gone with no clear same-domain clerk replacement
                                            -> ('', inconclusive, None)  [no downgrade]

    Mere ABSENCE of our address is never enough (a page may hide the email behind a
    contact form). We only assert drift when the page positively presents a
    plausible REPLACEMENT on the same domain. We never overwrite the stored email —
    a bad re-extraction must not corrupt the one channel that reaches the clerk.

    The fetch goes through the civic HTTP chokepoint (adaptive throttle/retry).
    """
    if not source_url:
        return "", "no source_url configured", None
    try:
        from ..http_client import civic_get
        resp = civic_get(source_url, timeout=20.0)
        if resp.status_code >= 400:
            return "", f"source page HTTP {resp.status_code}", None
        html = resp.text or ""
    except Exception as exc:  # network/parse hiccup — inconclusive, never downgrade
        return "", f"source fetch failed ({type(exc).__name__})", None

    found = {e.lower() for e in _PAGE_EMAIL_RE.findall(html)}
    if not found:
        return "", "no email found on source page", None

    stored = stored_email.strip().lower()
    if stored in found:
        return "verified", "stored clerk email still published on source page", stored

    # Stored address is absent. Only call this 'changed' if the page positively
    # lists a DIFFERENT plausible replacement on the SAME domain — strong evidence
    # the contact was reassigned, not just a reworded page. Two signals qualify:
    #   * a clerk/records-looking local part (works on busy staff pages), or
    #   * the page lists exactly ONE email and it's same-domain (the common shape
    #     of a dedicated open-records page, where the lone address IS the contact —
    #     catches a real reassignment even when the new local part isn't clerk-ish).
    domain = stored.split("@")[-1]
    same_domain = [e for e in found if e.split("@")[-1] == domain and e != stored]
    clerkish = [e for e in same_domain if any(k in e.split("@")[0] for k in _CLERKISH)]
    candidate = None
    if clerkish:
        candidate = clerkish[0]
    elif len(found) == 1 and same_domain:
        candidate = same_domain[0]
    if candidate:
        return ("changed",
                f"stored email not found; source page now lists {candidate}",
                candidate)
    return "", "stored email absent but no clear same-domain replacement", None


def _apply(conn, jid: int, name: str, new_status: str, reason: str, prev: str | None,
           observed: str | None = None) -> str | None:
    """Worst-wins merge + persist + flag a worsening transition. Returns the
    status actually written (or None on an inconclusive no-op).

    `observed` is the email the contact-source page most recently showed when it
    differs from ours (drift candidate). It's recorded as operator-review data
    regardless of the final status; the stored email itself is NEVER overwritten.
    """
    # Record the latest observed drift candidate even on an otherwise inconclusive
    # run — it's just data for the operator. Cleared when None so a resolved drift
    # doesn't leave a stale candidate behind.
    conn.execute(
        "UPDATE jurisdiction SET records_custodian_email_observed = %s WHERE id = %s",
        (observed, jid),
    )
    if not new_status:
        return None  # inconclusive lookup — leave prior status untouched
    # The bounce backstop: a real delivery failure is the hardest signal and the
    # monitor can't clear OR downgrade it — only a later successful send does
    # (handled in submit_comments). So an 'undeliverable' contact stays
    # undeliverable here even if validity/drift would read it lower.
    if prev == "undeliverable" and _RANK.get(new_status, 1) < _RANK["undeliverable"]:
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
        import json as _json
        ctx = {"jurisdiction_id": jid}
        if observed:
            ctx["observed_email"] = observed
        conn.execute(
            "INSERT INTO pipeline_failure (job_name, step, message, context) "
            "VALUES ('monitor_clerk_contact', %s, %s, %s::jsonb)",
            (new_status, f"{name}: clerk email {new_status} — {reason}",
             _json.dumps(ctx)),
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
            f"SELECT id, display_name, name, state_abbr, records_custodian_email, "
            f"records_custodian_email_status AS prev "
            f"FROM jurisdiction {where} ORDER BY id",
            params,
        ).fetchall()
        checked = flagged = drifted = 0
        for r in rows:
            email = r["records_custodian_email"]
            if not email:
                # No clerk email on file at all — can't deliver anything to them.
                status = _apply(conn, r["id"], r["display_name"], "unverified",
                                "no clerk email on file", r["prev"])
                if status:
                    flagged += 1
                continue

            # Signal 1 — validity (syntax + domain resolves; cheap, always run).
            new_v, reason_v = check_validity(email)

            # Signal 2 — drift: re-read the published email from the configured
            # contact-source page and compare. Bounce-anchored backstop stays in
            # submit_comments; this catches a change BEFORE a send fails.
            new_d, reason_d, observed = "", "", None
            source_url = _source_url_for(r["name"], r["state_abbr"])
            if source_url:
                new_d, reason_d, observed = check_drift(source_url, email)

            # Worst-wins: the highest-ranked non-empty status decides; 'verified'
            # is the best. Drift 'changed' (3) thus overrides a mere validity pass.
            cands = [(s, rsn) for s, rsn in ((new_v, reason_v), (new_d, reason_d)) if s]
            status_in, reason_in = (
                max(cands, key=lambda sr: _RANK.get(sr[0], 1)) if cands else ("", "inconclusive")
            )
            checked += 1
            status = _apply(
                conn, r["id"], r["display_name"], status_in, reason_in, r["prev"],
                observed=observed if new_d == "changed" else None,
            )
            if new_d == "changed":
                drifted += 1
            if status and status != "verified":
                flagged += 1
                print(f"  ⚠ {r['display_name']}: {status} — {reason_in} ({email})")
    print(f"--- clerk-contact check: {checked} checked, {flagged} need review, "
          f"{drifted} drift candidate(s) ---")
    return 0


def _source_url_for(name: str, state_abbr: str) -> str | None:
    """Resolve a jurisdiction's records_custodian.source_url from its config, by
    deriving the slug the same way the rest of the pipeline does. Returns None when
    no config or no source_url — in which case drift detection is simply skipped
    (bounce-anchored only) rather than guessing."""
    slug = f"{name.lower().replace(' ', '-')}-{state_abbr.lower()}"
    try:
        from ..jurisdiction import load_config
        cfg = load_config(slug)
    except Exception:
        return None
    return (cfg.get("records_custodian") or {}).get("source_url")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jurisdiction", help="slug like 'grovetown-ga'; default all")
    args = parser.parse_args()
    return run(args.jurisdiction)


if __name__ == "__main__":
    sys.exit(main())
