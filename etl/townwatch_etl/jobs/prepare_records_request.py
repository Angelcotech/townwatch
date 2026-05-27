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
import sys
from datetime import date
from pathlib import Path
from typing import Any

from ..audit import record_failure, state_law
from ..db import connect
from ..jurisdiction import load_config


# Location where rendered PDFs land — the web app serves them from /records-requests/.
# We resolve relative to this repo, so the prep job stays jurisdiction-agnostic.
_REPO_ROOT = Path(__file__).resolve().parents[3]   # .../townwatch/
_PDF_OUT_DIR = _REPO_ROOT.parent / "townwatch-web" / "public" / "records-requests"
_PDF_OUT_DIR.mkdir(parents=True, exist_ok=True)


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
}


def _human_date(d: date) -> str:
    return d.strftime("%B %-d, %Y") if d else "the earliest available date"


def _build_letter(
    finding_row: dict,
    body_row: dict,
    jurisdiction_cfg: dict,
    state_cfg: dict,
) -> dict:
    """Compose the letter dict consumed by docs/make_records_request_pdf.render()."""
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

    subject = tmpl["subject_template"].format(
        body_name=body_name, since_human=_human_date(since),
    )

    items = [
        s.format(
            city_full=city_full,
            body_name=body_name,
            since_human=_human_date(since),
            today_human=_human_date(today),
        )
        for s in tmpl["items_template"]
    ]

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
        "title": f"Open Records Act Request — {city_full} {body_name} {tmpl['record_type']}",
        "date": today.strftime("%B %-d, %Y"),
        "recipient": recipient,
        "delivery_note": f"Sent via: email to {custodian['email']} and U.S. Mail.",
        "subject": subject,
        "greeting": f"Dear {_greeting_last_name(custodian)},",
        "preamble": (
            f"Pursuant to the {ora['title']}, {ora['citation']}, I request access to and "
            f"copies of the following records:"
        ),
        "items": items,
        "body_paragraphs": boilerplate,
        "closing": "Thank you for your assistance. Please reply to:",
        "sender": sender,
        "signature_name": "David Brown",
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


def prepare_for_finding(conn, finding_id: int) -> dict[str, Any]:
    """Returns {'records_request_id': int, 'pdf_path': str, 'created': bool}.

    Idempotent — if a non-closed records_request already exists for this
    finding, returns it without re-rendering.
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

    letter = _build_letter(finding_dict, body_row, jurisdiction_cfg, state_cfg)

    # Render PDF
    from sys import path as _sys_path
    docs_dir = _REPO_ROOT / "docs"
    if str(docs_dir) not in _sys_path:
        _sys_path.insert(0, str(docs_dir))
    from make_records_request_pdf import render  # type: ignore

    today_str = date.today().isoformat()
    pdf_filename = f"finding-{finding_id}-{today_str}.pdf"
    pdf_full_path = _PDF_OUT_DIR / pdf_filename
    render(letter, pdf_full_path)
    pdf_rel = f"/records-requests/{pdf_filename}"

    # Insert records_request row
    row = conn.execute(
        """
        INSERT INTO records_request (
            finding_id, status, pdf_path, pdf_generated_at, meta
        )
        VALUES (%s, 'ready_for_review', %s, now(), %s::jsonb)
        RETURNING id
        """,
        (
            finding_id,
            pdf_rel,
            json.dumps({"category": f_row["category"], "letter_subject": letter["subject"]}),
        ),
    ).fetchone()
    return {"records_request_id": row["id"], "pdf_path": pdf_rel, "created": True}


def _jurisdiction_slug(display_name: str, state_abbr: str) -> str:
    return f"{display_name.lower().replace(' ', '-')}-{state_abbr.lower()}"


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--finding-id", type=int, help="Prepare for this finding only")
    group.add_argument("--all-open", action="store_true",
                       help="Prepare for every open finding without a non-closed request")
    args = parser.parse_args()

    with connect() as conn:
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
        print(f"Preparing requests for {len(ids)} finding(s)...")
        for fid in ids:
            try:
                result = prepare_for_finding(conn, fid)
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
