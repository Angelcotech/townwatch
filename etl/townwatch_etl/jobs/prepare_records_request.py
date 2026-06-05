"""
Prepare a records-request PDF + DB row for a given compliance_finding.

Idempotent: if a non-closed records_request already exists for the finding,
the job is a no-op (returns the existing row id). Otherwise it:

  1. Loads finding + body + jurisdiction config + state law config.
  2. Builds a letter dict from those.
  3. Renders the PDF via docs/make_records_request_pdf.render().
  4. Saves the PDF into ../townwatch-web/public/records-requests/.
  5. Inserts a records_request row with status='ready_for_review'.

Designed to be called automatically from refresh_findings.upsert_finding
when a new finding is inserted or reopened, OR run manually for one
finding via CLI:
    python -m townwatch_etl.jobs.prepare_records_request --finding-id 1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

from .. import activity
from ..audit import record_failure, state_law
from ..db import connect
from ..jurisdiction import load_config


_REPO_ROOT = Path(__file__).resolve().parents[3]   # .../townwatch/

# Where rendered records-request PDFs land. Locally this defaults to the
# sibling townwatch-web repo's static dir (the web app serves them from
# /records-requests/). In a container the ETL and web services DON'T share a
# filesystem, so this default writes to an ephemeral, unreachable path — set
# RECORDS_REQUEST_PDF_DIR to a real destination there.
#
# KNOWN GAP: even with a writable dir, cross-service *delivery* is unsolved —
# the web app has no route serving these PDFs yet, and a Railway volume isn't
# shared across services. The durable fix is to store the PDF bytes in the
# DB (the one shared resource) or in object storage. Tracked as an open
# design item; this env knob is the seam that fix will plug into.
_PDF_OUT_DIR = Path(
    os.environ.get(
        "RECORDS_REQUEST_PDF_DIR",
        str(_REPO_ROOT.parent / "townwatch-web" / "public" / "records-requests"),
    )
)


def _pdf_path(filename: str) -> Path:
    """Resolve a PDF output path, creating the output dir lazily. Done here
    rather than at import so merely importing this module has no filesystem
    side effect — importing it used to mkdir at import time, which in a
    root container silently created a junk /townwatch-web tree."""
    _PDF_OUT_DIR.mkdir(parents=True, exist_ok=True)
    return _PDF_OUT_DIR / filename


# Per-finding-category letter content. Each entry defines the request-specific
# language; the boilerplate (response deadline, fee cap, withholding clause)
# is shared across categories and pulled from state law config.
_CATEGORY_LETTER_CONFIG = {
    "minutes_missing": {
        "subject_template": (
            "Georgia Open Records Act request &mdash; "
            "{body_name} meeting minutes, {since_human}&ndash;present"
        ),
        "items_template": [
            (
                "All meeting minutes of the {city_full} {body_name} for meetings held during "
                "the period {since_human} through the present ({today_human})."
            ),
            (
                "For any {body_name} meeting in that period for which no written minutes have been "
                "prepared, a written statement to that effect identifying the meeting by date."
            ),
            (
                "If draft or unapproved minutes exist for any meeting in that period in lieu of "
                "final approved versions, please provide those drafts and identify them as such."
            ),
        ],
        "record_type": "meeting minutes",
    },
    "member_roster_missing": {
        "subject_template": (
            "Georgia Open Records Act request &mdash; "
            "{body_name} current membership and appointment records"
        ),
        "items_template": [
            (
                "The current roster of members serving on the {city_full} {body_name}, including "
                "for each member: full name, appointment date, term expiration date, and the seat "
                "or district the member occupies."
            ),
            (
                "Copies of the appointment instruments (council resolutions, mayoral appointments, "
                "or other official records) by which each current member of the {body_name} was "
                "seated."
            ),
            (
                "Any historical roster of members of the {body_name} maintained by the City Clerk's "
                "office, covering the period January 1, 2018 through the present."
            ),
        ],
        "record_type": "appointed-body membership records",
    },
    "meeting_notice_too_short": {
        "subject_template": (
            "Georgia Open Records Act request &mdash; "
            "{body_name} meeting-notice policy and short-notice records"
        ),
        "items_template": [
            (
                "The current written policy of the {city_full} governing how and when notice of "
                "{body_name} regular meetings is published to the public, including the minimum "
                "advance-notice period the City commits to provide for regular meetings."
            ),
            (
                "For every {body_name} regular meeting held during the period January 1, 2024 "
                "through the present for which notice was given fewer than three (3) calendar days "
                "in advance, a written explanation of the circumstances necessitating the short "
                "notice, including any communication between the City Clerk and the body members "
                "regarding the meeting's scheduling."
            ),
            (
                "Copies of the public meeting notices (agenda postings, press releases, website "
                "snapshots) actually issued for each such short-notice {body_name} meeting."
            ),
        ],
        "record_type": "meeting-notice policy and short-notice records",
    },
    "campaign_finance_missing": {
        "subject_template": (
            "Georgia Open Records Act request &mdash; "
            "{body_name} campaign finance filings, current officials"
        ),
        "items_template": [
            (
                "All Campaign Contribution Disclosure Reports (CCDRs) and any related campaign "
                "finance filings submitted under OCGA &sect; 21-5-34 by each current sitting "
                "member of the {city_full} {body_name}, for every election cycle in which that "
                "person ran for office, through the present. Include pre-election, post-election, "
                "semi-annual, and two-business-day reports of contributions in excess of $1,000."
            ),
            (
                "Any amendments, addenda, or supplemental filings to the above reports."
            ),
            (
                "For each current sitting member, a written statement identifying which required "
                "reports were filed and which were not filed, including the report period(s) missing."
            ),
            (
                "Any Personal Financial Disclosure Statements or related disclosures submitted to "
                "the City Clerk by current sitting members of the {body_name} during their service, "
                "to the extent any such records are within the City's custody."
            ),
            (
                "A statement of the format in which these records are maintained (paper, "
                "electronic, or both) and the process by which they are accepted and retained."
            ),
        ],
        "record_type": "campaign finance disclosures",
    },
    "budget_process_missing": {
        "subject_template": (
            "Georgia Open Records Act request &mdash; "
            "{body_name} adopted budget and budget-adoption records"
        ),
        "items_template": [
            (
                "The adopted annual budget (by ordinance or resolution) of the {city_full} "
                "{body_name} for the current fiscal year, including the adopting instrument and "
                "the date of adoption."
            ),
            (
                "The proposed budget as placed for public inspection under OCGA &sect; 36-81-5, "
                "and the notice of its availability."
            ),
            (
                "The notice(s) of the public hearing(s) held on the budget, and the minutes of "
                "those hearings."
            ),
            (
                "If no annual budget has yet been adopted for the current fiscal year, a written "
                "statement to that effect, identifying the date adoption is scheduled."
            ),
        ],
        "record_type": "adopted budget and budget-adoption records",
        # A missing annual budget isn't a per-meeting count; render a since-only line.
        "count_noun": None,
    },
}


def _human_date(d: date) -> str:
    return d.strftime("%B %-d, %Y") if d else "the earliest available date"


def _build_letter(
    finding_row: dict,
    body_row: dict,
    jurisdiction_cfg: dict,
    state_cfg: dict,
    tone: int = 1,
) -> dict:
    """Compose the letter dict consumed by docs/make_records_request_pdf.render().

    `tone` controls how warm vs formal the letter reads:
      1 = friendly  — leads with mission, thanks the custodian for their public
                      service, frames the ask as collaborative; the statute is
                      referenced as context rather than as a demand.
      2 = standard  — business-formal follow-up. Statute cited clearly, response
                      window mentioned politely, fewer warmth paragraphs.
      3 = strict    — formal legal demand with quoted statute, explicit deadlines,
                      and withholding-citation language. This is the prior default.

    The per-category items list is identical across tones — the documents being
    requested are the same regardless of how the cover letter reads.
    """
    if tone not in (1, 2, 3):
        raise ValueError(f"tone must be 1, 2, or 3; got {tone!r}")
    category = finding_row["category"]
    if category not in _CATEGORY_LETTER_CONFIG:
        raise ValueError(
            f"No letter template configured for finding category {category!r}. "
            f"Add an entry to _CATEGORY_LETTER_CONFIG."
        )
    tmpl = _CATEGORY_LETTER_CONFIG[category]

    j = jurisdiction_cfg["jurisdiction"]
    custodian = jurisdiction_cfg.get("records_custodian")
    if not custodian:
        raise ValueError(
            f"Jurisdiction {j['display_name']!r} has no records_custodian block. "
            f"Add one (name/honorific/title/email/mailing_address) before preparing requests."
        )

    state = state_cfg
    ora = state["open_records_act"]
    response_days = ora["response_deadline_business_days"]

    today = date.today()
    since = finding_row.get("since_date") or today
    body_name = body_row["name"]
    city_full = f"City of {j['display_name']}" if j["type"] == "city" else j["display_name"]

    items = [
        s.format(
            city_full=city_full,
            body_name=body_name,
            since_human=_human_date(since),
            today_human=_human_date(today),
        )
        for s in tmpl["items_template"]
    ]

    tone_block = _tone_block(
        tone=tone,
        custodian=custodian,
        city_full=city_full,
        body_name=body_name,
        record_type=tmpl["record_type"],
        ora=ora,
        response_days=response_days,
        category_subject_template=tmpl["subject_template"],
        since=since,
    )

    boilerplate = [
        (
            "I prefer to receive these records in electronic format (PDF, DOCX, or the format in "
            "which they are maintained) delivered by email to <b>[YOUR EMAIL]</b>, which avoids "
            f"per-page copying fees under {ora['fee_citation']}."
        ),
        (
            f"If your agency anticipates fees exceeding ${ora['fee_estimate_threshold_dollars']}.00 "
            f"under {ora['fee_citation']}, please provide a written cost estimate before incurring "
            "the cost. I will respond to the estimate within three business days."
        ),
        (
            f"Under {ora['response_deadline_citation']}, please respond to this request within "
            f"{response_days} ({_business_days_word(response_days)}) business days. If any portion "
            "of the request requires more time to fulfill, please advise in writing within that "
            "period of (a) the specific records that will require additional time, (b) the reason "
            f"for the delay, and (c) when the records will be produced. Under "
            f"{ora['no_records_statement_citation']}, if any responsive records do not exist or are "
            f"not in your custody, please state that in writing within the same {response_days} "
            "business days."
        ),
        (
            "If you withhold any responsive record in whole or in part, please cite the specific "
            "statute authorizing the withholding and identify the record being withheld with enough "
            f"specificity that I can evaluate the exemption. TownWatch is an independent civic "
            f"records research project documenting how local governments publish {tmpl['record_type']}; "
            "this request is part of that ongoing research."
        ),
    ]

    recipient = [
        f"{custodian.get('honorific','').strip()} {custodian['name']}".strip(),
        custodian["title"],
        *custodian["mailing_address"],
    ]

    sender = [
        "David Brown",
        "TownWatch — Civic Records Research",
        "[YOUR MAILING ADDRESS]",
        "[YOUR EMAIL]",
        "[YOUR PHONE]",
    ]

    return {
        "title": f"Records Request — {city_full} {body_name} {tmpl['record_type']}",
        "date": today.strftime("%B %-d, %Y"),
        "recipient": recipient,
        "delivery_note": f"Sent via: email to {custodian['email']} and U.S. Mail.",
        "subject": tone_block["subject"],
        "greeting": tone_block["greeting"],
        "preamble_paragraphs": tone_block["preamble_paragraphs"],
        "items": items,
        "body_paragraphs": tone_block["body_paragraphs"] if tone == 1 or tone == 2 else boilerplate,
        "closing": tone_block["closing"],
        "sender": sender,
        "signature_name": "David Brown",
        "signoff": tone_block["signoff"],
    }


def _tone_block(*, tone, custodian, city_full, body_name, record_type, ora, response_days, category_subject_template, since):
    """Tone-varying pieces of the letter. Returns dict with: subject,
    greeting, preamble_paragraphs (list), body_paragraphs (list),
    closing, signoff. Items list comes from the category template and
    is identical across tones."""
    first_name = (custodian["name"].split() or [custodian["name"]])[0]
    last_with_title = _greeting_last_name(custodian)
    # Bare category suffix (e.g. "Planning Commission meeting minutes, …")
    # — the tone-specific prefix is added below per level.
    category_subject = category_subject_template.format(
        body_name=body_name, since_human=_human_date(since),
    )
    # Strip any pre-baked "Open Records Act request — " prefix the
    # category template may carry over from earlier strict-only days.
    for legacy_prefix in (
        "Georgia Open Records Act request &mdash; ",
        "Open Records Act request &mdash; ",
    ):
        if category_subject.startswith(legacy_prefix):
            category_subject = category_subject[len(legacy_prefix):]
            break

    if tone == 1:
        return {
            "subject": f"Records Request from TownWatch &mdash; {category_subject}",
            "greeting": f"Hi {first_name},",
            "preamble_paragraphs": [
                # Mission
                (
                    "I'm reaching out from TownWatch, a nonpartisan civic-transparency "
                    "project that aggregates local government records so citizens can "
                    "see how their government works."
                ),
                # Gratitude
                (
                    f"Before I get to my request, I want to thank you for the work you "
                    f"do as {custodian['title']}. Records custodianship is some of the "
                    f"most important and least visible work in local government."
                ),
                # Disarm
                (
                    "This isn't an adversarial request. Where we find gaps in published "
                    "records, our goal is to help close them so citizens have a complete "
                    "picture."
                ),
                # Transition
                (
                    f"For the {city_full} {body_name}, would you be able to share the "
                    f"following?"
                ),
            ],
            "body_paragraphs": [
                (
                    "Electronic delivery to <b>[YOUR EMAIL]</b> works great — whatever "
                    "format is easiest for your office."
                ),
                (
                    f"For reference, this falls under {ora['title']} ({ora['citation']}). "
                    f"If any portion needs more time, just let us know what's reasonable."
                ),
            ],
            "closing": "Thank you again for your time. Please reply to:",
            "signoff": "With appreciation,",
        }

    if tone == 2:
        return {
            "subject": f"Records Request &mdash; {category_subject}",
            "greeting": f"Dear {last_with_title},",
            "preamble_paragraphs": [
                (
                    f"Following up on our prior correspondence regarding {city_full} "
                    f"{body_name} {record_type}."
                ),
                (
                    f"Under the {ora['title']}, {ora['citation']}, I'm requesting access to "
                    f"and copies of the following records:"
                ),
            ],
            "body_paragraphs": [
                (
                    "I'd appreciate electronic delivery (PDF, DOCX, or the format in which "
                    "the records are maintained) by email to <b>[YOUR EMAIL]</b>, which "
                    f"avoids per-page fees under {ora['fee_citation']}."
                ),
                (
                    f"Under {ora['response_deadline_citation']}, the statutory response "
                    f"window is {response_days} ({_business_days_word(response_days)}) "
                    f"business days. If any portion will take longer to compile, please "
                    f"let me know in writing what's reasonable and I'll work with your "
                    f"timeline."
                ),
                (
                    f"TownWatch is an independent civic-records research project documenting "
                    f"how local governments publish {record_type}."
                ),
            ],
            "closing": "Thank you for your attention to this follow-up. Please reply to:",
            "signoff": "Sincerely,",
        }

    # tone == 3 (strict — original formal demand)
    return {
        "subject": f"{ora['title'].split(',')[0]} request &mdash; {category_subject}",
        "greeting": f"Dear {last_with_title},",
        "preamble_paragraphs": [
            (
                f"Pursuant to the {ora['title']}, {ora['citation']}, I request access to "
                f"and copies of the following records:"
            ),
        ],
        # body_paragraphs is built outside (the strict boilerplate is the same
        # 4-paragraph block prepare_records_request has always emitted).
        "body_paragraphs": None,
        "closing": "Thank you for your assistance. Please reply to:",
        "signoff": "Sincerely,",
    }


def _business_days_word(n: int) -> str:
    return {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 7: "seven", 10: "ten"}.get(n, str(n))


def _greeting_last_name(custodian: dict) -> str:
    parts = custodian["name"].split()
    last = parts[-1] if parts else custodian["name"]
    honorific = custodian.get("honorific", "").strip()
    if honorific in ("Hon.", "Honorable"):
        return f"{honorific} {last}"
    # Default to Mr./Ms. — operator can edit in admin UI before sending.
    return f"Mr./Ms. {last}"


def prepare_for_finding(conn, finding_id: int, *, tone: int = 1) -> dict[str, Any]:
    """Returns {'records_request_id': int, 'pdf_path': str, 'created': bool}.

    Idempotent — if a non-closed records_request already exists for this
    finding, returns it without re-rendering. Use regenerate_request to
    re-render an existing row at a new tone.
    """
    # Skip if a non-closed request already exists.
    existing = conn.execute(
        """
        SELECT id, pdf_path FROM records_request
        WHERE finding_id = %s AND status IN ('draft','ready_for_review','sent','responded')
        ORDER BY created_at DESC LIMIT 1
        """,
        (finding_id,),
    ).fetchone()
    if existing:
        return {
            "records_request_id": existing["id"],
            "pdf_path": existing["pdf_path"],
            "created": False,
        }

    # Load finding + body + jurisdiction
    f_row = conn.execute(
        """
        SELECT cf.id, cf.category, cf.since_date, cf.statute_label,
               gb.id AS body_id, gb.name AS body_name, gb.body_type,
               j.id AS jurisdiction_id, j.display_name AS jurisdiction_name,
               j.state_abbr
        FROM compliance_finding cf
        JOIN governing_body gb ON gb.id = cf.governing_body_id
        JOIN jurisdiction j ON j.id = gb.jurisdiction_id
        WHERE cf.id = %s
        """,
        (finding_id,),
    ).fetchone()
    if f_row is None:
        raise ValueError(f"compliance_finding {finding_id} not found")

    # Look up jurisdiction slug from FIPS — we name configs by slug
    j_fips = conn.execute(
        "SELECT fips_code FROM jurisdiction WHERE id = %s", (f_row["jurisdiction_id"],),
    ).fetchone()
    slug = _jurisdiction_slug(f_row["jurisdiction_name"], f_row["state_abbr"])
    jurisdiction_cfg = load_config(slug)
    state_cfg = state_law(f_row["state_abbr"])

    body_row = {"name": f_row["body_name"], "body_type": f_row["body_type"]}
    finding_dict = dict(f_row)

    letter = _build_letter(finding_dict, body_row, jurisdiction_cfg, state_cfg, tone=tone)

    # Render PDF
    from sys import path as _sys_path
    docs_dir = _REPO_ROOT / "docs"
    if str(docs_dir) not in _sys_path:
        _sys_path.insert(0, str(docs_dir))
    from make_records_request_pdf import render  # type: ignore

    today_str = date.today().isoformat()
    pdf_filename = f"finding-{finding_id}-{today_str}.pdf"
    pdf_full_path = _pdf_path(pdf_filename)
    render(letter, pdf_full_path)
    pdf_rel = f"/records-requests/{pdf_filename}"

    # Insert records_request row
    row = conn.execute(
        """
        INSERT INTO records_request (
            finding_id, status, tone, pdf_path, pdf_generated_at, meta
        )
        VALUES (%s, 'ready_for_review', %s, %s, now(), %s::jsonb)
        RETURNING id
        """,
        (
            finding_id,
            tone,
            pdf_rel,
            json.dumps({"category": f_row["category"], "letter_subject": letter["subject"]}),
        ),
    ).fetchone()
    activity.record(
        conn, f_row["jurisdiction_id"], "records_request_generated",
        title=f"Records request generated — {f_row['body_name']} ({f_row['category']})",
        ref_kind="records_request", ref_id=str(row["id"]), once=True,
        meta={"finding_id": finding_id, "category": f_row["category"], "tone": tone},
    )
    return {"records_request_id": row["id"], "pdf_path": pdf_rel, "created": True}


def regenerate_request(conn, request_id: int, *, tone: int) -> dict[str, Any]:
    """Re-render the PDF for an existing records_request at a new tone.
    Used by the admin escalation flow when the operator decides the
    clerk needs a firmer follow-up. Updates pdf_path + tone in place,
    leaves status alone (a Sent request stays Sent — escalation doesn't
    reset the clock, it just produces the PDF you'd send next)."""
    if tone not in (1, 2, 3):
        raise ValueError(f"tone must be 1, 2, or 3; got {tone!r}")

    rr = conn.execute(
        """
        SELECT rr.id, rr.finding_id, rr.tone AS current_tone,
               cf.category, cf.since_date, cf.statute_label,
               gb.id AS body_id, gb.name AS body_name, gb.body_type,
               j.id AS jurisdiction_id, j.display_name AS jurisdiction_name,
               j.state_abbr
        FROM records_request rr
        JOIN compliance_finding cf ON cf.id = rr.finding_id
        JOIN governing_body gb ON gb.id = cf.governing_body_id
        JOIN jurisdiction j ON j.id = gb.jurisdiction_id
        WHERE rr.id = %s
        """,
        (request_id,),
    ).fetchone()
    if rr is None:
        raise ValueError(f"records_request {request_id} not found")

    slug = _jurisdiction_slug(rr["jurisdiction_name"], rr["state_abbr"])
    jurisdiction_cfg = load_config(slug)
    state_cfg = state_law(rr["state_abbr"])
    body_row = {"name": rr["body_name"], "body_type": rr["body_type"]}
    finding_dict = dict(rr)

    letter = _build_letter(finding_dict, body_row, jurisdiction_cfg, state_cfg, tone=tone)

    from sys import path as _sys_path
    docs_dir = _REPO_ROOT / "docs"
    if str(docs_dir) not in _sys_path:
        _sys_path.insert(0, str(docs_dir))
    from make_records_request_pdf import render  # type: ignore

    today_str = date.today().isoformat()
    pdf_filename = f"finding-{rr['finding_id']}-tone{tone}-{today_str}.pdf"
    pdf_full_path = _pdf_path(pdf_filename)
    render(letter, pdf_full_path)
    pdf_rel = f"/records-requests/{pdf_filename}"

    conn.execute(
        """
        UPDATE records_request
        SET tone = %s, pdf_path = %s, pdf_generated_at = now(), updated_at = now()
        WHERE id = %s
        """,
        (tone, pdf_rel, request_id),
    )
    return {"records_request_id": request_id, "pdf_path": pdf_rel, "tone": tone}


def _jurisdiction_slug(display_name: str, state_abbr: str) -> str:
    return f"{display_name.lower().replace(' ', '-')}-{state_abbr.lower()}"


# =====================================================================
# Consolidated (per-jurisdiction) records request
# =====================================================================
#
# Rolls all currently-open findings for a jurisdiction into one PDF
# addressed to the records custodian. One email to the clerk instead
# of N emails, one per finding. Items are grouped under per-body
# section headings ("For the Planning Commission:") with global numeric
# ordering so the clerk responds to one numbered list.

def _concise_item(f: dict, tmpl: dict, today: date) -> str:
    """One tight line naming exactly what's missing, for the friendly draft:
    e.g. 'Meeting minutes — 3 meetings, March 12, 2026 to present.'"""
    rtype = tmpl.get("record_type", f["category"].replace("_", " "))
    head = rtype[:1].upper() + rtype[1:]
    count = f.get("count") or 0
    since = f.get("since_date")
    # Most findings count meetings; categories where a per-meeting count is
    # meaningless (e.g. a missing annual budget adoption) set count_noun=None to
    # suppress the count clause and render a since-only line.
    noun = tmpl.get("count_noun", "meetings")
    if since and count > 1 and noun:
        return f"{head} — {count} {noun}, {_human_date(since)} to present."
    if since:
        return f"{head} — none on record since {_human_date(since)}."
    return f"{head}."


def _build_consolidated_letter(
    jurisdiction_cfg: dict,
    findings: list[dict],
    state_cfg: dict,
    tone: int = 1,
) -> dict:
    if tone not in (1, 2, 3):
        raise ValueError(f"tone must be 1, 2, or 3; got {tone!r}")
    if not findings:
        raise ValueError("findings list is empty — nothing to consolidate")

    j = jurisdiction_cfg["jurisdiction"]
    custodian = jurisdiction_cfg.get("records_custodian")
    if not custodian:
        raise ValueError(
            f"Jurisdiction {j['display_name']!r} has no records_custodian block."
        )
    ora = state_cfg["open_records_act"]
    response_days = ora["response_deadline_business_days"]
    today = date.today()
    city_full = f"City of {j['display_name']}" if j["type"] == "city" else j["display_name"]
    first_name = (custodian["name"].split() or [custodian["name"]])[0]
    last_with_title = _greeting_last_name(custodian)

    # Group findings by body, preserving DB-order so PC/BZA/Council
    # show up in a stable sequence across regenerations.
    by_body: dict[tuple, list[dict]] = {}
    for f in findings:
        key = (f["body_id"], f["body_name"], f["body_type"])
        by_body.setdefault(key, []).append(f)

    # Build sections — one per body. Each section's items concatenate
    # the per-category templates for every finding under that body.
    sections: list[dict] = []
    for (body_id, body_name, body_type), body_findings in by_body.items():
        items: list[str] = []
        for f in body_findings:
            tmpl = _CATEGORY_LETTER_CONFIG.get(f["category"])
            if not tmpl:
                continue
            if tone == 1:
                # Friendly default: a tight one-line "what's missing" per item, so
                # the clerk can scan the ask at a glance. The thorough, formally
                # enumerated version is reserved for the escalated tones below.
                items.append(_concise_item(f, tmpl, today))
            else:
                since = f.get("since_date") or today
                items.extend(
                    s.format(
                        city_full=city_full,
                        body_name=body_name,
                        since_human=_human_date(since),
                        today_human=_human_date(today),
                    )
                    for s in tmpl["items_template"]
                )
        if items:
            sections.append({
                "heading": f"For the {body_name}:",
                "items": items,
            })

    framing = _consolidated_tone_block(
        tone=tone,
        custodian=custodian,
        city_full=city_full,
        first_name=first_name,
        last_with_title=last_with_title,
        ora=ora,
        response_days=response_days,
    )

    recipient = [
        f"{custodian.get('honorific','').strip()} {custodian['name']}".strip(),
        custodian["title"],
        *custodian["mailing_address"],
    ]
    sender = [
        "David Brown",
        "TownWatch — Civic Records Research",
        "[YOUR MAILING ADDRESS]",
        "[YOUR EMAIL]",
        "[YOUR PHONE]",
    ]

    return {
        "title": f"Records Request — {city_full}",
        "date": today.strftime("%B %-d, %Y"),
        "recipient": recipient,
        "delivery_note": f"Sent via: email to {custodian['email']} and U.S. Mail.",
        "subject": framing["subject"],
        "greeting": framing["greeting"],
        "preamble_paragraphs": framing["preamble_paragraphs"],
        "sections": sections,
        "body_paragraphs": framing["body_paragraphs"],
        "closing": framing["closing"],
        "sender": sender,
        "signature_name": "David Brown",
        "signoff": framing["signoff"],
    }


def _consolidated_tone_block(*, tone, custodian, city_full, first_name, last_with_title, ora, response_days):
    """Tone-varying framing for a per-jurisdiction (multi-body) letter.
    Mirrors _tone_block but with a jurisdiction-level transition since
    multiple bodies are covered."""
    if tone == 1:
        return {
            "subject": f"Records Request from TownWatch &mdash; {city_full}",
            "greeting": f"Hi {first_name},",
            "preamble_paragraphs": [
                (
                    "I'm reaching out from TownWatch, a nonpartisan civic-transparency "
                    "project that aggregates local government records so citizens can "
                    "see how their government works."
                ),
                (
                    f"Before I get to my request, I want to thank you for the work you "
                    f"do as {custodian['title']}. Records custodianship is some of the "
                    f"most important and least visible work in local government."
                ),
                (
                    "This isn't an adversarial request. Where we find gaps in published "
                    "records, our goal is to help close them so citizens have a complete "
                    "picture."
                ),
                (
                    f"For the {city_full}, would you be able to share the following?"
                ),
            ],
            "body_paragraphs": [
                (
                    "Electronic delivery to <b>[YOUR EMAIL]</b> works great — whatever "
                    "format is easiest for your office."
                ),
                (
                    f"For reference, this falls under {ora['title']} ({ora['citation']}). "
                    f"If any portion needs more time, just let us know what's reasonable."
                ),
            ],
            "closing": "Thank you again for your time. Please reply to:",
            "signoff": "With appreciation,",
        }
    if tone == 2:
        return {
            "subject": f"Records Request &mdash; {city_full}",
            "greeting": f"Dear {last_with_title},",
            "preamble_paragraphs": [
                (
                    f"Following up on prior correspondence regarding {city_full} "
                    f"records."
                ),
                (
                    f"Under the {ora['title']}, {ora['citation']}, I'm requesting access "
                    f"to and copies of the following records:"
                ),
            ],
            "body_paragraphs": [
                (
                    "I'd appreciate electronic delivery (PDF, DOCX, or the format in "
                    "which the records are maintained) by email to <b>[YOUR EMAIL]</b>, "
                    f"which avoids per-page fees under {ora['fee_citation']}."
                ),
                (
                    f"Under {ora['response_deadline_citation']}, the statutory response "
                    f"window is {response_days} ({_business_days_word(response_days)}) "
                    f"business days. If any portion will take longer to compile, please "
                    f"let me know what's reasonable."
                ),
            ],
            "closing": "Thank you for your attention to this follow-up. Please reply to:",
            "signoff": "Sincerely,",
        }
    # tone == 3 strict
    return {
        "subject": f"{ora['title'].split(',')[0]} request &mdash; {city_full}",
        "greeting": f"Dear {last_with_title},",
        "preamble_paragraphs": [
            (
                f"Pursuant to the {ora['title']}, {ora['citation']}, I request access "
                f"to and copies of the following records:"
            ),
        ],
        "body_paragraphs": [
            (
                "I prefer to receive these records in electronic format (PDF, DOCX, or the format in "
                "which they are maintained) delivered by email to <b>[YOUR EMAIL]</b>, which avoids "
                f"per-page copying fees under {ora['fee_citation']}."
            ),
            (
                f"If your agency anticipates fees exceeding ${ora['fee_estimate_threshold_dollars']}.00 "
                f"under {ora['fee_citation']}, please provide a written cost estimate before incurring "
                "the cost. I will respond to the estimate within three business days."
            ),
            (
                f"Under {ora['response_deadline_citation']}, please respond to this request within "
                f"{response_days} ({_business_days_word(response_days)}) business days. If any portion "
                "of the request requires more time to fulfill, please advise in writing within that "
                "period of (a) the specific records that will require additional time, (b) the reason "
                f"for the delay, and (c) when the records will be produced. Under "
                f"{ora['no_records_statement_citation']}, if any responsive records do not exist or are "
                f"not in your custody, please state that in writing within the same {response_days} "
                "business days."
            ),
            (
                "If you withhold any responsive record in whole or in part, please cite the specific "
                "statute authorizing the withholding and identify the record being withheld with enough "
                "specificity that I can evaluate the exemption."
            ),
        ],
        "closing": "Thank you for your assistance. Please reply to:",
        "signoff": "Sincerely,",
    }


def _drop_resolved(conn, findings: list[dict]) -> list[dict]:
    """Re-run the audit observers right now and keep only findings still observed.
    A finding can resolve between the periodic refresh and the moment a draft is
    composed (the clerk posted the record, or it was a transient mis-read). Re-using
    the same observers refresh_findings uses (SQL-only, no spend) keeps a draft from
    ever asking for something that's already available."""
    from .refresh_findings import OBSERVERS  # lazy: avoid import cycle at module load
    # Re-observe once per body, collect the categories still flagged.
    observed_by_body: dict[int, set] = {}
    for f in findings:
        body_id = f["body_id"]
        if body_id in observed_by_body:
            continue
        cats: set = set()
        for obs in OBSERVERS:
            try:
                r = obs(conn, body_id, f["state_abbr"], f["body_type"])
            except Exception:
                r = None  # an observer error must not silently drop a real gap
            if r is not None:
                cats.add(r.category)
        observed_by_body[body_id] = cats
    kept = []
    for f in findings:
        if f["category"] in observed_by_body.get(f["body_id"], set()):
            kept.append(f)
        else:
            print(f"   pre-draft re-audit: dropped resolved finding {f['id']} "
                  f"({f['category']} / body {f['body_id']})")
    return kept


def ensure_consolidated_request(conn, jurisdiction_id: int, *, tone: int = 1) -> dict[str, Any] | None:
    """Idempotent — ensure ONE consolidated records_request covers all
    currently-open findings for the jurisdiction. Returns None when no
    open findings exist.

    Behavior:
      - No existing non-closed request → create one with all open findings.
      - Existing request with the same finding_id set → noop.
      - Existing request with different finding_ids → regenerate the PDF
        and update finding_ids in place (keeps the existing tone).
    """
    findings = conn.execute(
        """
        SELECT cf.id, cf.category, cf.since_date, cf.statute_label, cf.count,
               gb.id AS body_id, gb.name AS body_name, gb.body_type,
               j.id AS jurisdiction_id, j.display_name AS jurisdiction_name,
               j.state_abbr
        FROM compliance_finding cf
        JOIN governing_body gb ON gb.id = cf.governing_body_id
        JOIN jurisdiction j ON j.id = gb.jurisdiction_id
        WHERE j.id = %s AND cf.status = 'open'
        ORDER BY gb.id, cf.id
        """,
        (jurisdiction_id,),
    ).fetchall()
    if not findings:
        return None

    findings = [dict(f) for f in findings]
    # Pre-draft re-audit: confirm each item is STILL missing right now before we
    # ask the clerk for it — never request a record that's since been posted or
    # that we mis-detected. If everything resolved, propose no draft at all.
    findings = _drop_resolved(conn, findings)
    if not findings:
        return None
    finding_ids = [f["id"] for f in findings]
    j_row = findings[0]

    slug = _jurisdiction_slug(j_row["jurisdiction_name"], j_row["state_abbr"])
    jurisdiction_cfg = load_config(slug)
    state_cfg = state_law(j_row["state_abbr"])

    # Look for an existing non-closed consolidated request for this
    # jurisdiction. We match by overlap with any of the open findings —
    # an old per-finding request OR a previous consolidated one both
    # qualify, since we'll either supersede or refresh.
    existing = conn.execute(
        """
        SELECT id, finding_ids, tone, status
        FROM records_request
        WHERE status IN ('draft', 'ready_for_review', 'sent', 'responded')
          AND finding_ids && %s::int[]
        ORDER BY array_length(finding_ids, 1) DESC NULLS LAST, created_at DESC
        LIMIT 1
        """,
        (finding_ids,),
    ).fetchone()

    # If the existing request already covers exactly this set of findings,
    # no work needed.
    if existing and set(existing["finding_ids"] or []) == set(finding_ids):
        return {
            "records_request_id": existing["id"],
            "created": False,
            "updated": False,
            "finding_ids": finding_ids,
        }

    effective_tone = existing["tone"] if existing else tone
    letter = _build_consolidated_letter(jurisdiction_cfg, findings, state_cfg, tone=effective_tone)

    from sys import path as _sys_path
    docs_dir = _REPO_ROOT / "docs"
    if str(docs_dir) not in _sys_path:
        _sys_path.insert(0, str(docs_dir))
    from make_records_request_pdf import render  # type: ignore

    today_str = date.today().isoformat()
    pdf_filename = f"jurisdiction-{jurisdiction_id}-tone{effective_tone}-{today_str}.pdf"
    pdf_full_path = _pdf_path(pdf_filename)
    render(letter, pdf_full_path)
    pdf_rel = f"/records-requests/{pdf_filename}"

    if existing:
        conn.execute(
            """
            UPDATE records_request
            SET finding_id      = %s,
                finding_ids     = %s,
                pdf_path        = %s,
                pdf_generated_at = now(),
                updated_at      = now()
            WHERE id = %s
            """,
            (finding_ids[0], finding_ids, pdf_rel, existing["id"]),
        )
        return {
            "records_request_id": existing["id"],
            "created": False,
            "updated": True,
            "finding_ids": finding_ids,
        }

    row = conn.execute(
        """
        INSERT INTO records_request (
            finding_id, finding_ids, status, tone, pdf_path, pdf_generated_at, meta
        )
        VALUES (%s, %s, 'ready_for_review', %s, %s, now(), %s::jsonb)
        RETURNING id
        """,
        (
            finding_ids[0],
            finding_ids,
            effective_tone,
            pdf_rel,
            json.dumps({
                "consolidated": True,
                "letter_subject": letter["subject"],
                "jurisdiction_id": jurisdiction_id,
            }),
        ),
    ).fetchone()
    activity.record(
        conn, jurisdiction_id, "records_request_generated",
        title=f"Consolidated records request generated ({len(finding_ids)} findings)",
        ref_kind="records_request", ref_id=str(row["id"]), once=True,
        meta={"finding_ids": finding_ids, "consolidated": True, "tone": effective_tone},
    )
    return {
        "records_request_id": row["id"],
        "created": True,
        "updated": False,
        "finding_ids": finding_ids,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--finding-id", type=int, help="Prepare for this finding only")
    group.add_argument("--all-open", action="store_true",
                       help="Prepare for every open finding without a non-closed request")
    group.add_argument("--regenerate-request-id", type=int,
                       help="Re-render an existing records_request at the given --tone")
    group.add_argument("--regenerate-all", action="store_true",
                       help="Re-render every non-closed records_request at the given --tone")
    group.add_argument("--consolidate-jurisdiction-id", type=int,
                       help="Ensure one consolidated records_request covers all open findings for this jurisdiction")
    group.add_argument("--consolidate-all", action="store_true",
                       help="Run --consolidate-jurisdiction-id for every jurisdiction with open findings")
    parser.add_argument("--tone", type=int, default=1, choices=(1, 2, 3),
                        help="Tone level: 1=friendly (default), 2=standard, 3=strict")
    args = parser.parse_args()

    with connect() as conn:
        if args.consolidate_jurisdiction_id:
            result = ensure_consolidated_request(conn, args.consolidate_jurisdiction_id, tone=args.tone)
            if result is None:
                print(f"  · jurisdiction {args.consolidate_jurisdiction_id}: no open findings")
            else:
                tag = "✓ created" if result["created"] else ("↻ updated" if result["updated"] else "= unchanged")
                print(f"  {tag}  request {result['records_request_id']} covering {len(result['finding_ids'])} finding(s)")
            return 0
        if args.consolidate_all:
            jurisdictions = conn.execute(
                """
                SELECT DISTINCT j.id, j.display_name
                FROM jurisdiction j
                JOIN governing_body gb ON gb.jurisdiction_id = j.id
                JOIN compliance_finding cf ON cf.governing_body_id = gb.id
                WHERE cf.status = 'open'
                ORDER BY j.display_name
                """,
            ).fetchall()
            print(f"Consolidating requests for {len(jurisdictions)} jurisdiction(s) with open findings...")
            for jr in jurisdictions:
                try:
                    result = ensure_consolidated_request(conn, jr["id"], tone=args.tone)
                    if result is None:
                        print(f"  · {jr['display_name']}: no open findings")
                    else:
                        tag = "✓ created" if result["created"] else ("↻ updated" if result["updated"] else "= unchanged")
                        print(f"  {tag}  {jr['display_name']}: request {result['records_request_id']} ({len(result['finding_ids'])} finding(s))")
                except Exception as e:
                    print(f"  ✗ {jr['display_name']}: {type(e).__name__}: {e}")
            return 0
        if args.regenerate_request_id:
            result = regenerate_request(conn, args.regenerate_request_id, tone=args.tone)
            print(f"  ✓ regenerated request {result['records_request_id']} at tone {result['tone']} → {result['pdf_path']}")
            return 0
        if args.regenerate_all:
            rows = conn.execute(
                "SELECT id FROM records_request WHERE status <> 'closed' ORDER BY id",
            ).fetchall()
            print(f"Regenerating {len(rows)} non-closed request(s) at tone {args.tone}...")
            for r in rows:
                try:
                    result = regenerate_request(conn, r["id"], tone=args.tone)
                    print(f"  ✓ request {result['records_request_id']} → {result['pdf_path']}")
                except Exception as e:
                    print(f"  ✗ request {r['id']}: {type(e).__name__}: {e}")
            return 0
        if args.finding_id:
            ids = [args.finding_id]
        else:
            rows = conn.execute(
                """
                SELECT cf.id FROM compliance_finding cf
                WHERE cf.status = 'open'
                  AND NOT EXISTS (
                    SELECT 1 FROM records_request rr
                    WHERE rr.finding_id = cf.id
                      AND rr.status IN ('draft','ready_for_review','sent','responded')
                  )
                ORDER BY cf.opened_at
                """,
            ).fetchall()
            ids = [r["id"] for r in rows]
        print(f"Preparing requests for {len(ids)} finding(s) at tone {args.tone}...")
        for fid in ids:
            try:
                result = prepare_for_finding(conn, fid, tone=args.tone)
                tag = "✓ created" if result["created"] else "⊘ already exists"
                print(f"  {tag}  finding {fid} → request {result['records_request_id']} ({result['pdf_path']})")
            except Exception as e:
                record_failure(
                    conn,
                    job_name="prepare_records_request",
                    step="prepare_for_finding",
                    finding_id=fid,
                    message=f"{type(e).__name__}: {e}",
                    exception=e,
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
