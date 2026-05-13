"""
Extract structured votes and decisions from meeting-minutes PDFs.

Tiered approach (June pattern):
  1. pdfplumber → text layer (digital PDFs, free, instant)
  2. ocrmypdf + pdfplumber (scanned PDFs, local CPU, no API cost)
  3. Claude vision on the raw PDF (fallback when Tiers 1-2 fail)

When text extraction succeeds, we call Claude Haiku 4.5 with the text.
When it fails, we fall back to Sonnet 4.6 with the PDF as a document
block. Either path returns the same Pydantic-validated structure, so
downstream callers don't care which tier ran.

Five extraction priorities (in order):
  1. Substantive decisions (ordinances, resolutions, zoning, contracts)
  2. Individual votes (who voted yes/no/abstain/recused)
  3. Recusals — declared conflicts of interest (gold)
  4. Public comment per item (speakers + stance)
  5. Plain-English summaries (citizen-readable)
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from ..config import ANTHROPIC_API_KEY
from .pdf_text import extract_text


# =====================================================================
# Pydantic schema — what Claude is constrained to return
# =====================================================================

VoteValue = Literal["yes", "no", "abstain", "absent", "conflict_recusal"]
MotionType = Literal[
    "ordinance", "resolution", "zoning_change", "budget_amendment",
    "appointment", "contract_approval", "procedural", "other",
]
Outcome = Literal["passed", "failed", "tabled", "withdrawn", "no_action"]
MeetingType = Literal["regular", "special", "workshop", "emergency", "executive_session"]
Stance = Literal["for", "against", "neutral", "unclear"]
Confidence = Literal["high", "medium", "low"]


class MeetingMeta(BaseModel):
    date: str = Field(description="YYYY-MM-DD format")
    body_name: str = Field(description="The governing body (e.g. 'City Council')")
    meeting_type: MeetingType
    extraction_confidence: Confidence


class Attendance(BaseModel):
    present: list[str] = Field(description="Names exactly as written in the minutes")
    absent: list[str] = Field(default_factory=list)


class IndividualVote(BaseModel):
    name: str = Field(description="Exact name as written in the minutes")
    vote: VoteValue
    notes: str | None = Field(default=None, description="e.g. 'recused due to conflict'")


class Recusal(BaseModel):
    name: str
    reason: str | None = Field(default=None, description="Verbatim reason if stated")


class PublicCommentEntry(BaseModel):
    speaker: str = Field(description="Name + role/address as given")
    stance: Stance
    summary: str = Field(description="One-line gist of their comment")


class VoteTally(BaseModel):
    yes: int = 0
    no: int = 0
    abstain: int = 0
    absent: int = 0


class AgendaItem(BaseModel):
    item_number: str | None = Field(default=None, description="e.g. '7A'")
    title: str
    motion_text_verbatim: str | None = Field(
        default=None,
        description="Exact text of the motion if quoted in the minutes",
    )
    summary_plain_english: str = Field(
        description="1-2 sentence plain-English explanation of what this decision does"
    )
    motion_type: MotionType
    movant: str | None = Field(default=None, description="Who moved the motion")
    seconder: str | None = Field(default=None, description="Who seconded")
    outcome: Outcome
    vote_tally: VoteTally
    individual_votes: list[IndividualVote]
    recusals: list[Recusal] = Field(default_factory=list)
    public_comment: list[PublicCommentEntry] = Field(default_factory=list)
    source_page: int = Field(description="1-indexed PDF page where this item begins")


class MeetingExtraction(BaseModel):
    meeting: MeetingMeta
    attendance: Attendance
    agenda_items: list[AgendaItem]
    extraction_notes: str = Field(
        default="",
        description="Ambiguities, illegible passages, or unusual items. Empty string if clean.",
    )


# =====================================================================
# Prompts
# =====================================================================

_RULES = """\
WHAT TO EXTRACT
- Substantive decisions: ordinances, resolutions, zoning changes, contracts, appointments, budget actions
- Individual votes (who voted yes/no/abstain/recused)
- Recusals and declared conflicts of interest — the most important data in the document
- Public comment on specific items (speakers + their stance)
- A 1-2 sentence plain-English summary explaining what each decision actually does, written for a citizen with no legal background

WHAT TO SKIP
- Approval of previous minutes
- Calls to order, recesses, motions to adjourn
- Generic "public comment period" entries without item-specific context
- Discussion that did not result in a vote

RULES
1. Names: Copy exactly as written. Do not normalize spellings or capitalization. Identity resolution happens downstream.
2. Motion text: Copy verbatim when quoted (motion_text_verbatim). Always write your own plain-English summary (summary_plain_english) separately.
3. Vote tally must equal the count of individual_votes. If you cannot reconcile, note it in extraction_notes.
4. Recusal language to watch for: "recuse", "abstain due to conflict", "left the room", "did not participate", "declared a conflict", or similar. When someone recused on an item, mark their individual_vote as "conflict_recusal" AND add an entry in recusals.
5. Roll-call votes: if individual votes are listed, transcribe them. If only a tally is given (e.g. "Motion passed 4-0"), distribute votes among present members and mark absent members as "absent".
6. Public comment stance: use your judgment based on what they said. Use "unclear" if it's not obvious.
7. Source page: PDF page (1-indexed) where the item begins. The text below contains "--- PAGE BREAK ---" markers; count pages starting from 1.
8. If a field is illegible or unclear, use null/None. Do not guess.
9. extraction_confidence: "high" if every item was clear, "medium" if some details were unclear, "low" if significant content was illegible.
"""

TEXT_INSTRUCTIONS = f"""\
You are extracting structured data from City Council meeting minutes. The text below was produced by OCR or extracted from the PDF's text layer. Some OCR errors are possible — read carefully, especially numbers and names.

Read the entire document and return the structured output described by the schema.

{_RULES}

MEETING MINUTES TEXT (page breaks marked):
"""

VISION_INSTRUCTIONS = f"""\
You are extracting structured data from a scanned City Council meeting minutes PDF.

Read the entire document carefully and return the structured output described by the schema.

{_RULES}

Now extract from the attached PDF.
"""

# Models — text path uses fast/cheap Haiku, vision fallback uses capable Sonnet
TEXT_MODEL = "claude-haiku-4-5"
VISION_MODEL = "claude-sonnet-4-6"


# =====================================================================
# API calls
# =====================================================================

def extract_from_pdf(pdf_path: Path) -> tuple[MeetingExtraction, str]:
    """
    Returns (extraction, method) where method is 'text_layer' | 'ocr' | 'vision'.

    Tries text-based extraction (Tier 1 + Tier 2) first. Only falls back
    to Claude vision when local text extraction yields nothing usable.
    """
    text_result = extract_text(pdf_path)
    if text_result.text:
        return _extract_from_text(text_result.text), text_result.method
    return _extract_from_pdf_vision(pdf_path), "vision"


def _extract_from_text(text: str) -> MeetingExtraction:
    """Cheap path: Haiku reads OCR text + emits structured JSON."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.parse(
        model=TEXT_MODEL,
        max_tokens=16384,
        messages=[{
            "role": "user",
            "content": TEXT_INSTRUCTIONS + "\n" + text,
        }],
        output_format=MeetingExtraction,
    )
    return response.parsed_output


def _extract_from_pdf_vision(pdf_path: Path) -> MeetingExtraction:
    """Fallback: Claude reads the scanned PDF directly via multimodal vision."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    pdf_b64 = base64.standard_b64encode(pdf_path.read_bytes()).decode("utf-8")
    response = client.messages.parse(
        model=VISION_MODEL,
        max_tokens=16384,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": pdf_b64,
                    },
                },
                {"type": "text", "text": VISION_INSTRUCTIONS},
            ],
        }],
        output_format=MeetingExtraction,
    )
    return response.parsed_output
