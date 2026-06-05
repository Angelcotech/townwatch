"""Apply Resend delivery-webhook events to clerk-contact health.

A real bounce or spam-complaint is the HARDEST deliverability signal there is — it
means a message actually failed to reach the records custodian, the single address
records requests and comment digests go to. A 'delivered' event is the opposite:
positive confirmation the address works. This maps those events onto
jurisdiction.records_custodian_email_status (keyed by recipient email) so the dev
dashboard surfaces a broken clerk contact the MOMENT Resend tells us — the async
backstop to the periodic validity/drift monitor (jobs/monitor_clerk_contact.py).

Called by the intake API (POST /email/delivery-event), which the Next.js Resend
webhook forwards to after verifying the Svix signature. The web layer never writes
Postgres directly; this module performs the write.
"""

from __future__ import annotations

import json

from .db import connect

# Resend event type → the contact status it implies. Only these three carry a
# deliverability verdict; every other event type is acknowledged and ignored.
_EVENT_STATUS = {
    "email.bounced": "undeliverable",
    "email.complained": "undeliverable",
    "email.delivered": "verified",
}


def record_delivery_event(email: str, event_type: str) -> dict:
    """Apply one Resend delivery event to every jurisdiction whose records-custodian
    email matches `email`. Returns a small summary for the caller/logs.

    A bounce/complaint sets 'undeliverable' (and flags the operator queue once, on
    the worsening transition). A 'delivered' sets 'verified' — and legitimately
    CLEARS a prior 'undeliverable', because an actual delivery confirmation is the
    authoritative outcome (unlike the periodic monitor, which may not clear a bounce).
    Unmatched emails and non-verdict events are no-ops.
    """
    status = _EVENT_STATUS.get(event_type)
    if status is None:
        return {"matched": 0, "ignored": True, "event_type": event_type}
    addr = (email or "").strip().lower()
    if not addr:
        return {"matched": 0, "reason": "no email"}

    matched = 0
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, display_name, records_custodian_email_status AS prev "
            "FROM jurisdiction WHERE LOWER(records_custodian_email) = %s",
            (addr,),
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE jurisdiction SET records_custodian_email_status = %s, "
                "records_custodian_email_checked_at = now() WHERE id = %s",
                (status, r["id"]),
            )
            # Operator-only signal, once, on the worsening transition into a bounce.
            if status == "undeliverable" and r["prev"] != "undeliverable":
                conn.execute(
                    "INSERT INTO pipeline_failure (job_name, step, message, context) "
                    "VALUES ('resend_webhook', %s, %s, %s::jsonb)",
                    (
                        event_type,
                        f"{r['display_name']}: clerk email {event_type} — a send did not reach {addr}",
                        json.dumps({"jurisdiction_id": r["id"], "email": addr}),
                    ),
                )
            matched += 1
    return {"matched": matched, "status": status, "event_type": event_type}
