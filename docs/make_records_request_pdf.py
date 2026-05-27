"""
Generate a records-request PDF from a structured letter dict.

Reusable across jurisdictions and record types. Each letter is described
as a Python dict; the script renders a clean business-letter PDF with
a small TownWatch header.

Run:
    python docs/make_records_request_pdf.py
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
)
from reportlab.lib import colors


HEADER_TITLE = "TownWatch"
HEADER_SUBTITLE = "Civic Records Research"

DOCS_DIR = Path(__file__).resolve().parent


def _styles():
    base = getSampleStyleSheet()
    return {
        "header_title": ParagraphStyle(
            "header_title", parent=base["Normal"],
            fontName="Helvetica-Bold", fontSize=14, leading=16,
            alignment=TA_LEFT, textColor=colors.HexColor("#0f172a"),
        ),
        "header_subtitle": ParagraphStyle(
            "header_subtitle", parent=base["Normal"],
            fontName="Helvetica", fontSize=9, leading=11,
            alignment=TA_LEFT, textColor=colors.HexColor("#475569"),
        ),
        "header_meta": ParagraphStyle(
            "header_meta", parent=base["Normal"],
            fontName="Helvetica", fontSize=9, leading=11,
            alignment=TA_RIGHT, textColor=colors.HexColor("#475569"),
        ),
        "body": ParagraphStyle(
            "body", parent=base["Normal"],
            fontName="Times-Roman", fontSize=11, leading=14,
            spaceAfter=10, alignment=TA_LEFT,
        ),
        "date": ParagraphStyle(
            "date", parent=base["Normal"],
            fontName="Times-Roman", fontSize=11, leading=14,
            spaceAfter=18,
        ),
        "addr": ParagraphStyle(
            "addr", parent=base["Normal"],
            fontName="Times-Roman", fontSize=11, leading=14, spaceAfter=2,
        ),
        "subject": ParagraphStyle(
            "subject", parent=base["Normal"],
            fontName="Times-Bold", fontSize=11, leading=14,
            spaceBefore=10, spaceAfter=14,
        ),
        "numbered": ParagraphStyle(
            "numbered", parent=base["Normal"],
            fontName="Times-Roman", fontSize=11, leading=14,
            leftIndent=24, bulletIndent=8, spaceAfter=8,
        ),
        "sig": ParagraphStyle(
            "sig", parent=base["Normal"],
            fontName="Times-Roman", fontSize=11, leading=14, spaceAfter=2,
        ),
        "section_heading": ParagraphStyle(
            "section_heading", parent=base["Normal"],
            fontName="Times-Bold", fontSize=11, leading=14,
            spaceBefore=10, spaceAfter=6,
        ),
    }


def _build_header(s):
    """Two-column header: brand on left, document type on right."""
    brand = [
        Paragraph(HEADER_TITLE, s["header_title"]),
        Paragraph(HEADER_SUBTITLE, s["header_subtitle"]),
    ]
    meta = [
        Paragraph("Open Records Act Request", s["header_meta"]),
        Paragraph("Public Records Custodian", s["header_meta"]),
    ]
    t = Table([[brand, meta]], colWidths=[3.5 * inch, 3.0 * inch])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return [
        t,
        Spacer(1, 0.08 * inch),
        HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#94a3b8")),
        Spacer(1, 0.25 * inch),
    ]


def render(letter: dict, output_path: Path) -> Path:
    """
    letter dict shape:
      {
        "date": "May 22, 2026",
        "recipient": [...],
        "delivery_note": "Sent via: email to ... and U.S. Mail.",
        "subject": "Records Request from TownWatch — ...",
        "greeting": "Hi Vicki,",
        # Paragraphs rendered between greeting and items. Use a list to
        # carry multiple paragraphs (mission + gratitude + disarm for
        # friendly tone). String form is accepted for backward compat
        # with the demo letters at the bottom of this file.
        "preamble_paragraphs": ["mission para", "gratitude para", ...],
        # OR legacy single-string form:
        "preamble": "Pursuant to OCGA § 50-18-70 et seq., I request...",
        "items": ["Numbered item 1 text", ...],
        "body_paragraphs": [str, str, ...],
        "closing": "Thank you for your assistance. Please reply to:",
        "sender": [...],
        # Signoff line above the signature (default "Sincerely,")
        "signoff": "With appreciation,",
        "signature_name": "David Brown",
        "title": "PDF document title",
      }
    """
    s = _styles()
    doc = SimpleDocTemplate(
        str(output_path), pagesize=LETTER,
        leftMargin=1.0 * inch, rightMargin=1.0 * inch,
        topMargin=0.85 * inch, bottomMargin=1.0 * inch,
        title=letter.get("title", "Open Records Act Request"),
    )
    story = []
    story.extend(_build_header(s))
    story.append(Paragraph(letter["date"], s["date"]))
    for line in letter["recipient"]:
        story.append(Paragraph(line, s["addr"]))
    story.append(Spacer(1, 0.15 * inch))
    if letter.get("delivery_note"):
        story.append(Paragraph(f"<i>{letter['delivery_note']}</i>", s["body"]))
    story.append(Paragraph("RE: " + letter["subject"], s["subject"]))
    story.append(Paragraph(letter["greeting"], s["body"]))
    # Multi-paragraph preamble (preferred) OR single-string preamble (legacy).
    preamble_paragraphs = letter.get("preamble_paragraphs")
    if preamble_paragraphs is None and letter.get("preamble"):
        preamble_paragraphs = [letter["preamble"]]
    for para in preamble_paragraphs or []:
        story.append(Paragraph(para, s["body"]))
    # Items can be rendered either as one flat numbered list (single-finding
    # request) OR as multiple sections each with a heading + numbered items
    # (consolidated multi-finding request). When `sections` is present the
    # numbering is GLOBAL across sections — the clerk sees one numbered list
    # to respond to, with body-name headings as orientation.
    sections = letter.get("sections")
    if sections:
        n = 0
        for section in sections:
            story.append(Paragraph(section["heading"], s["section_heading"]))
            for item in section["items"]:
                n += 1
                story.append(Paragraph(f"{n}.&nbsp;&nbsp;{item}", s["numbered"]))
    else:
        for i, item in enumerate(letter.get("items", []), start=1):
            story.append(Paragraph(f"{i}.&nbsp;&nbsp;{item}", s["numbered"]))
    for para in letter["body_paragraphs"]:
        story.append(Paragraph(para, s["body"]))
    story.append(Paragraph(letter["closing"], s["body"]))
    for line in letter["sender"]:
        story.append(Paragraph(line, s["sig"]))
    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph(letter.get("signoff", "Sincerely,"), s["body"]))
    story.append(Spacer(1, 0.5 * inch))
    story.append(Paragraph(letter["signature_name"], s["body"]))
    doc.build(story)
    return output_path


# =====================================================================
# Letters to render — add new dicts here for new request types
# =====================================================================

GROVETOWN_RECIPIENT = [
    "Hon. Vicki Capetillo",
    "City Clerk",
    "City of Grovetown",
    "103 Old Wrightsboro Road",
    "Grovetown, GA 30813",
]

GROVETOWN_DELIVERY = "Sent via: email to clerk@cityofgrovetown.com and U.S. Mail."

SENDER_BLOCK = [
    "David Brown",
    "TownWatch — Civic Records Research",
    "[YOUR MAILING ADDRESS]",
    "[YOUR EMAIL]",
    "[YOUR PHONE]",
]

# Shared closing paragraphs (response deadline, fee cap, exemption-citation, withholding)
def _standard_response_paragraphs(record_type: str) -> list[str]:
    return [
        (
            "I prefer to receive these records in electronic format (PDF, DOCX, or the format in which "
            "they are maintained) delivered by email to <b>[YOUR EMAIL]</b>, which avoids per-page "
            "copying fees under OCGA &sect; 50-18-71(d)."
        ),
        (
            "If your agency anticipates fees exceeding $25.00 under OCGA &sect; 50-18-71(d), please "
            "provide a written cost estimate before incurring the cost. I will respond to the "
            "estimate within three business days."
        ),
        (
            "Under OCGA &sect; 50-18-71(b)(1)(A), please respond to this request within three (3) "
            "business days. If any portion of the request requires more time to fulfill, please "
            "advise in writing within that period of (a) the specific records that will require "
            "additional time, (b) the reason for the delay, and (c) when the records will be "
            "produced. Under OCGA &sect; 50-18-71(b)(1)(B), if any responsive records do not "
            "exist or are not in your custody, please state that in writing within the same three "
            "business days."
        ),
        (
            "If you withhold any responsive record in whole or in part, please cite the specific OCGA "
            "section authorizing the withholding and identify the record being withheld with enough "
            f"specificity that I can evaluate the exemption. TownWatch is an independent civic records "
            f"research project documenting how local governments publish {record_type}; this request "
            "is part of that ongoing research."
        ),
    ]


pc_minutes = {
    "title": "Open Records Act Request — Grovetown Planning Commission Minutes",
    "date": "May 22, 2026",
    "recipient": GROVETOWN_RECIPIENT,
    "delivery_note": GROVETOWN_DELIVERY,
    "subject": "Georgia Open Records Act request &mdash; Planning Commission meeting minutes, 2023&ndash;present",
    "greeting": "Dear Ms. Capetillo,",
    "preamble": (
        "Pursuant to the Georgia Open Records Act, OCGA &sect; 50-18-70 et seq., I request access "
        "to and copies of the following records:"
    ),
    "items": [
        (
            "All meeting minutes of the City of Grovetown Planning Commission for meetings held during "
            "the period January 1, 2023 through the present (May 22, 2026)."
        ),
        (
            "For any Planning Commission meeting in that period for which no written minutes have been "
            "prepared, a written statement to that effect identifying the meeting by date."
        ),
        (
            "If draft or unapproved minutes exist for any meeting in that period in lieu of final "
            "approved versions, please provide those drafts and identify them as such."
        ),
    ],
    "body_paragraphs": _standard_response_paragraphs("meeting minutes"),
    "closing": "Thank you for your assistance. Please reply to:",
    "sender": SENDER_BLOCK,
    "signature_name": "David Brown",
}


campaign_finance = {
    "title": "Open Records Act Request — Grovetown Campaign Finance Filings",
    "date": "May 22, 2026",
    "recipient": GROVETOWN_RECIPIENT,
    "delivery_note": GROVETOWN_DELIVERY,
    "subject": "Georgia Open Records Act request &mdash; Municipal candidate campaign finance filings, 2018&ndash;present",
    "greeting": "Dear Ms. Capetillo,",
    "preamble": (
        "Pursuant to the Georgia Open Records Act, OCGA &sect; 50-18-70 et seq., and consistent "
        "with the filing requirements of OCGA &sect; 21-5-34 governing municipal candidate "
        "campaign disclosures, I request access to and copies of the following records:"
    ),
    "items": [
        (
            "All Campaign Contribution Disclosure Reports (CCDRs) and any related campaign finance "
            "filings submitted under OCGA &sect; 21-5-34 by candidates for the offices of Mayor and "
            "City Council Member of the City of Grovetown for the election cycles of 2018, 2020, "
            "2022, and 2024, including any pre-election, post-election, semi-annual, and two-business-day "
            "reports of contributions in excess of $1,000."
        ),
        (
            "Any amendments, addenda, or supplemental filings to the above reports."
        ),
        (
            "For each election cycle listed, a written statement identifying which candidates were "
            "required to file under OCGA &sect; 21-5-34 and which of those candidates did not file "
            "one or more required reports, including the report period(s) missing."
        ),
        (
            "Any Personal Financial Disclosure Statements or related disclosures submitted to the "
            "City Clerk by sitting elected officials of the City of Grovetown during the period "
            "January 1, 2018 through the present that are within the City's custody."
        ),
        (
            "A statement of the format in which these records are maintained (paper, electronic, or "
            "both) and the City's process for accepting and retaining them."
        ),
    ],
    "body_paragraphs": _standard_response_paragraphs("campaign finance disclosures"),
    "closing": "Thank you for your assistance. Please reply to:",
    "sender": SENDER_BLOCK,
    "signature_name": "David Brown",
}


bza_minutes = {
    "title": "Open Records Act Request — Grovetown Board of Zoning Appeals Minutes",
    "date": "May 22, 2026",
    "recipient": GROVETOWN_RECIPIENT,
    "delivery_note": GROVETOWN_DELIVERY,
    "subject": "Georgia Open Records Act request &mdash; Board of Zoning Appeals meeting minutes, 2022&ndash;present",
    "greeting": "Dear Ms. Capetillo,",
    "preamble": (
        "Pursuant to the Georgia Open Records Act, OCGA &sect; 50-18-70 et seq., I request access "
        "to and copies of the following records:"
    ),
    "items": [
        (
            "All meeting minutes of the City of Grovetown Board of Zoning Appeals for meetings "
            "held during the period January 1, 2022 through the present (May 22, 2026)."
        ),
        (
            "For any Board of Zoning Appeals meeting in that period for which no written minutes "
            "have been prepared, a written statement to that effect identifying the meeting by date."
        ),
        (
            "If draft or unapproved minutes exist for any meeting in that period in lieu of final "
            "approved versions, please provide those drafts and identify them as such."
        ),
    ],
    "body_paragraphs": _standard_response_paragraphs("meeting minutes"),
    "closing": "Thank you for your assistance. Please reply to:",
    "sender": SENDER_BLOCK,
    "signature_name": "David Brown",
}


LETTERS = {
    "records-request-grovetown-pc.pdf":              pc_minutes,
    "records-request-grovetown-bza.pdf":             bza_minutes,
    "records-request-grovetown-campaign-finance.pdf": campaign_finance,
}


def main() -> int:
    for filename, letter in LETTERS.items():
        out = DOCS_DIR / filename
        render(letter, out)
        size = out.stat().st_size
        print(f"  ✓ {filename}  ({size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
