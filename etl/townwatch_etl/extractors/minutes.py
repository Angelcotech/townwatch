"""
Extract structured votes and decisions from meeting-minutes PDFs.

Two-tier approach:
  1. pdfplumber → text layer (digital PDFs, free, instant)
  2. Claude vision on the raw PDF (everything else — scanned or fallback)

We deliberately do NOT use OCR (ocrmypdf/Tesseract) as a middle tier
because OCR introduces character-level errors that cascade through
identity resolution — every OCR typo becomes a duplicate "official"
record. Claude vision reads the original glyphs and uses semantic
context to disambiguate. Cost is higher per PDF but data integrity
is the binding constraint, not cost.

Five extraction priorities (in order):
  1. Substantive decisions (ordinances, resolutions, zoning, contracts)
  2. Individual votes (who voted yes/no/abstain/recused)
  3. Recusals — declared conflicts of interest (gold)
  4. Public comment per item (speakers + stance)
  5. Plain-English summaries (citizen-readable)
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from ..config import ANTHROPIC_API_KEY, VISION_RENDER_DPI
from .chunking import extend_unique
from .mistral_ocr import ocr_pdf
from ..llm_client import record_anthropic
from .pdf_text import extract_text
from .rasterize import vision_content
from .recovery import ExtractionReport, build_source, extract_with_ladder


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
    called_to_order_at: str | None = Field(default=None, description="HH:MM if recorded")
    adjourned_at: str | None = Field(default=None, description="HH:MM if recorded")
    extraction_confidence: Confidence


class Attendance(BaseModel):
    present: list[str] = Field(description="Elected member names, exactly as written")
    absent: list[str] = Field(default_factory=list)
    staff_present: list[str] = Field(
        default_factory=list,
        description='Non-elected staff/appointed officials: City Administrator, City '
                    'Attorney, department directors, clerk, etc. Format as "Title Name" '
                    '(e.g. "City Administrator Elaine Matthews", "Planning Director Smith").',
    )
    others_present: list[str] = Field(
        default_factory=list,
        description="Non-staff others recorded as present: county officials, state reps, "
                    "guests recognized by the chair, etc. Skip generic public-attendee mentions.",
    )


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
    # Identity + content
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

    # Upstream attribution — who brought this item and who recommended it
    petitioner: str | None = Field(
        default=None,
        description="Who requested/applied for this item — developer, business, "
                    "resident, or staff (e.g. 'Acme Development LLC', 'John Smith, owner')",
    )
    staff_recommender: str | None = Field(
        default=None,
        description="Staff member who recommended approval or presented the staff "
                    "report (e.g. 'Planning Director Smith', 'City Administrator Matthews')",
    )
    presenter: str | None = Field(
        default=None,
        description="Who actually presented the item to council if stated; often the "
                    "same as staff_recommender or the petitioner",
    )

    # Council action
    movant: str | None = Field(default=None, description="Who moved the motion")
    seconder: str | None = Field(default=None, description="Who seconded")
    outcome: Outcome
    vote_tally: VoteTally
    individual_votes: list[IndividualVote]
    recusals: list[Recusal] = Field(default_factory=list)

    # Deliberation + context
    discussion_summary: str | None = Field(
        default=None,
        description="1-3 sentence summary of any council deliberation, debate, or "
                    "questioning before the vote. Skip if no discussion is recorded.",
    )
    public_comment: list[PublicCommentEntry] = Field(default_factory=list)

    # Structured fields
    dollar_amount: float | None = Field(
        default=None,
        description="Dollar value stated for this item (contract value, budget amount, "
                    "fee, expenditure). Use the most specific number, not a budget total.",
    )
    documents_referenced: list[str] = Field(
        default_factory=list,
        description="Reports, plans, attachments, or studies referenced by this item",
    )
    locations: list[str] = Field(
        default_factory=list,
        description="Every property location mentioned, one entry per distinct location. "
                    "Use the original label as written — include the full string with "
                    "address, parcel ID, and any descriptive prefix "
                    '(e.g. "1110 Dodge Lane (Parcel ID 070 009)").',
    )

    source_page: int = Field(description="1-indexed PDF page where this item begins")


class MeetingExtraction(BaseModel):
    meeting: MeetingMeta
    attendance: Attendance
    agenda_items: list[AgendaItem]
    document_summary: str = Field(
        default="",
        description=(
            "2-3 sentence plain-English summary of what actually happened at the meeting. "
            "Written for a citizen who has not opened the PDF. Lead with the most "
            "consequential outcomes (what passed, what failed, what was tabled). Name "
            "dollar amounts, applicants, and recusals when notable. Example: 'Council "
            "passed a $2.4M road resurfacing contract 4-1 with Councilor Smith dissenting, "
            "tabled a rezoning at 1110 Dodge Lane after 45 minutes of public comment, and "
            "appointed two new members to the BZA. Mayor Jones recused on the rezoning.'"
        ),
    )
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
- UPSTREAM ATTRIBUTION: who petitioned/applied for the item, which staff member recommended it, who presented it
- Discussion summary: 1-3 sentences capturing any council deliberation before the vote
- Dollar amounts, document references, and every property address/parcel mentioned

WHAT TO SKIP
- Approval of previous minutes
- Calls to order, recesses, motions to adjourn
- Generic "public comment period" entries without item-specific context
- Discussion that did not result in a vote

ATTENDANCE EXTRACTION
- attendance.present and attendance.absent: ELECTED council/board members only (write names exactly as they appear)
- attendance.staff_present: every non-elected official noted as attending — City Administrator, City Attorney, City Clerk, Finance Director, Planning Director, Public Works Director, etc. Capture name + title.
- attendance.others_present: non-staff others recorded as present — county officials, state representatives, guests recognized by the chair. Skip the generic public audience.

DOCUMENT SUMMARY
- Always populate document_summary with a 2-3 sentence plain-English overview of what happened. This is what a citizen will read instead of opening the PDF. Lead with consequential outcomes (what passed/failed/tabled/withdrew) and notable details (dollar amounts, applicants, recusals).
- Be concrete and specific. Avoid hedging language unless the record itself is ambiguous.
- Skip procedural boilerplate (roll call, minutes approval, adjournment) unless something unusual happened during those steps.

RULES
1. Names: Copy exactly as written. Do not normalize spellings or capitalization. Identity resolution happens downstream.
2. Motion text: Copy verbatim when quoted (motion_text_verbatim). Always write your own plain-English summary (summary_plain_english) separately.
3. Vote tally must equal the count of individual_votes. If you cannot reconcile, note it in extraction_notes.
4. Recusal language to watch for: "recuse", "abstain due to conflict", "left the room", "did not participate", "declared a conflict", or similar. When someone recused on an item, mark their individual_vote as "conflict_recusal" AND add an entry in recusals.
5. Roll-call votes: if individual votes are listed, transcribe them. If only a tally is given (e.g. "Motion passed 4-0"), distribute votes among present members and mark absent members as "absent".
6. Public comment stance: use your judgment based on what they said. Use "unclear" if it's not obvious.
7. Source page: PDF page (1-indexed) where the item begins.
8. petitioner: the person/entity who REQUESTED or APPLIED for this item. Common signals: "On the application of...", "Petition filed by...", "Request from...", "Submitted by...". For internal items (staff-initiated ordinances), the petitioner may be a department or staff member.
9. staff_recommender: the staff member who explicitly recommended a course of action, presented a staff report, or whose recommendation is cited (e.g. "Staff recommends approval", "Per Planning Director Smith's memo"). Capture name + title when both stated.
10. presenter: who actually presented the item to council if recorded (often the same as staff_recommender or the petitioner). Skip when not stated.
11. discussion_summary: 1-3 sentence neutral summary of the council deliberation. If no discussion is recorded, leave null. Do NOT paraphrase or editorialize.
12. dollar_amount: the most specific dollar value stated for the item (contract value, budget appropriation, fee, expenditure). Not the year's total budget — the value of THIS decision.
13. documents_referenced: report names, attachments, exhibits, plans, or studies cited in the discussion (e.g. "Planning Commission report dated 4/3", "Engineer's letter from McGill").
14. locations: every property address, parcel ID, or described area mentioned. One entry per distinct location with the original label + extracted address/parcel_id when present.
15. If a field is illegible or unclear, use null/None. Do not guess.
16. extraction_confidence: "high" if every item was clear, "medium" if some details were unclear, "low" if significant content was illegible.
"""

def _schema_for_prompt() -> str:
    """Generate a compact JSON-schema description for the prompt. Strips the
    bulky $defs/refs Pydantic generates so the model sees the shape directly."""
    import json as _json
    s = MeetingExtraction.model_json_schema()
    return _json.dumps(s, indent=2)


OUTPUT_FORMAT_RULES = """\
Return exactly one JSON object matching the schema below. Do not include any
prose, markdown fences, or text outside the JSON. The response should BEGIN
with `{` and END with `}`. Use null for unknown/illegible fields. Use empty
arrays for empty lists.
"""

TEXT_INSTRUCTIONS = f"""\
You are extracting structured data from City Council meeting minutes. The text below was produced by OCR or extracted from the PDF's text layer. Some OCR errors are possible — read carefully, especially numbers and names.

{_RULES}

{OUTPUT_FORMAT_RULES}

JSON SCHEMA:
{_schema_for_prompt()}

MEETING MINUTES TEXT (page breaks marked):
"""

VISION_INSTRUCTIONS = f"""\
You are extracting structured data from a scanned City Council meeting minutes PDF.

Read the entire document carefully.

{_RULES}

{OUTPUT_FORMAT_RULES}

JSON SCHEMA:
{_schema_for_prompt()}

Now extract from the attached PDF.
"""

# Models — text path uses fast/cheap Haiku, vision fallback uses capable Sonnet
TEXT_MODEL = "claude-haiku-4-5"
VISION_MODEL = "claude-sonnet-4-6"

# Extraction-cache version. BUMP THIS when the prompt, schema, or models above
# change in a way that should re-extract existing documents — a bump invalidates
# all cached minutes (they re-extract under the new version on next process).
# Keep the model names in it so a model swap is a visible, intentional re-spend.
EXTRACTOR_VERSION = f"minutes-v1:{TEXT_MODEL}+{VISION_MODEL}"


# =====================================================================
# API calls
# =====================================================================

def extract_from_pdf(pdf_path: Path) -> tuple[MeetingExtraction, str, ExtractionReport]:
    """
    Returns (extraction, method, report).

    Runs through the escalating-filter ladder (recovery.extract_with_ladder):
    the document is split into page-windows and each window is resolved by
    primary extract → retry → sub-chunk → cross-strategy → pdf-repair, with
    irreducible windows classified as anomalies in the returned report. The
    text-layer path is preferred when the PDF has real text; otherwise vision.
    We never OCR locally — OCR errors break identity resolution.
    """
    text_layer = extract_text_layer_only(pdf_path)
    method = "text_layer"
    if text_layer is None:
        # Scanned: Mistral OCR is the primary path — cheaper, faster, and more
        # complete than vision. If OCR yields nothing, the source has no text
        # layer and the ladder falls back to vision per-window.
        text_layer = ocr_pdf(pdf_path)
        method = "ocr" if text_layer else "vision"
    source = build_source(pdf_path, text_layer)
    extraction, report = extract_with_ladder(
        source,
        text_window_fn=_extract_text_window,
        vision_window_fn=_extract_vision_window,
        merge_fn=_merge_extractions,
    )
    report.method = method
    return extraction, method, report


def extract_text_layer_only(pdf_path: Path) -> str | None:
    """Tier 1 only: pdfplumber on the existing text layer. None if no real text."""
    from .pdf_text import _extract_text_layer, CONTENT_CHAR_THRESHOLD
    try:
        text, _pages, content_chars = _extract_text_layer(pdf_path)
    except Exception:
        return None
    if content_chars < CONTENT_CHAR_THRESHOLD:
        return None
    return text


def _parse_json_response(response) -> MeetingExtraction:
    """Extract JSON from Claude's text response and validate against the Pydantic schema.

    The prompt instructs the model to output bare JSON, but we defensively
    locate the first `{` and last `}` in case the model wraps in fences or prose.
    """
    import json as _json
    import re as _re

    text = ""
    for block in response.content:
        if getattr(block, "type", "") == "text":
            text = block.text
            break

    # Strip markdown fences if present
    fence_match = _re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, _re.DOTALL)
    if fence_match:
        text = fence_match.group(1)

    # Locate the outermost JSON object
    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last == -1 or last < first:
        # Surface stop_reason so a starved/truncated response is diagnosable
        # rather than a cryptic empty string. stop_reason == 'max_tokens'
        # means the budget was exhausted (often by thinking) before any JSON.
        stop = getattr(response, "stop_reason", "?")
        raise ValueError(f"No JSON object found in response (stop_reason={stop}): {text[:200]!r}")
    payload = text[first : last + 1]
    data = _json.loads(payload)
    return MeetingExtraction.model_validate(data)


def _extract_text_window(text: str) -> MeetingExtraction:
    """Extract one window of document text (Haiku → JSON). The page-window
    chunker (_extract_from_text) maps this over large documents.

    Streamed with a 32K output budget: long minutes produced more JSON than
    the old 16384 non-streaming cap allowed, truncating it into invalid JSON
    (JSONDecodeError). Above ~21K tokens the SDK requires streaming, so we
    stream and collect the final message (same shape as a non-streamed one).
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    with client.messages.stream(
        model=TEXT_MODEL,
        max_tokens=32000,
        messages=[
            {"role": "user", "content": TEXT_INSTRUCTIONS + "\n" + text},
        ],
    ) as stream:
        response = stream.get_final_message()
    record_anthropic(TEXT_MODEL, response.usage)
    return _parse_json_response(response)


def _extract_vision_window(pdf_path: Path, dpi: int | None = VISION_RENDER_DPI) -> MeetingExtraction:
    """Extract one window sub-PDF via Claude vision. The page-window chunker
    maps this over large documents.

    Streamed with a 32K budget — the vision path runs adaptive thinking + high
    effort, whose thinking tokens count against max_tokens; streaming lifts the
    SDK's 10-min non-stream guard so the larger budget fits. ``dpi`` controls
    rasterization: None ships the raw PDF (default), an int rasterizes pages to
    images at that resolution (smaller payload, lower latency). Defaults to
    config.VISION_RENDER_DPI; the sweep passes an explicit value."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    with client.messages.stream(
        model=VISION_MODEL,
        max_tokens=32000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        messages=[{"role": "user", "content": vision_content(pdf_path, VISION_INSTRUCTIONS, dpi=dpi)}],
    ) as stream:
        response = stream.get_final_message()
    record_anthropic(VISION_MODEL, response.usage)
    return _parse_json_response(response)


def _merge_extractions(partials: list[tuple[MeetingExtraction, int]]) -> MeetingExtraction:
    """Reduce per-window extractions into one whole-document extraction.
    Items are offset-corrected to document page numbers and de-duplicated by
    (page, title); attendance lists are unioned; meeting metadata comes from
    the first window; the document summary is synthesized from the windows."""
    first = partials[0][0]
    items: list[AgendaItem] = []
    seen: set[tuple[int, str]] = set()
    present: list[str] = []
    absent: list[str] = []
    staff: list[str] = []
    others: list[str] = []
    notes: list[str] = []
    summaries: list[str] = []

    for ext, offset in partials:
        for it in ext.agenda_items:
            it.source_page = it.source_page + offset
            key = (it.source_page, (it.title or "").strip().casefold())
            if key in seen:
                continue
            seen.add(key)
            items.append(it)
        extend_unique(present, ext.attendance.present)
        extend_unique(absent, ext.attendance.absent)
        extend_unique(staff, ext.attendance.staff_present)
        extend_unique(others, ext.attendance.others_present)
        if ext.extraction_notes.strip():
            notes.append(ext.extraction_notes.strip())
        if ext.document_summary.strip():
            summaries.append(ext.document_summary.strip())

    items.sort(key=lambda it: it.source_page)
    return MeetingExtraction(
        meeting=first.meeting,
        attendance=Attendance(present=present, absent=absent, staff_present=staff, others_present=others),
        agenda_items=items,
        document_summary=_synthesize_summary(summaries),
        extraction_notes=" | ".join(notes)[:2000],
    )


def _synthesize_summary(summaries: list[str]) -> str:
    """Collapse per-window summaries into one 2-3 sentence whole-meeting
    summary (one cheap Haiku call). Falls back to the longest window summary
    if the call fails, so a reduce hiccup never loses the summary."""
    summaries = [s for s in summaries if s.strip()]
    if not summaries:
        return ""
    if len(summaries) == 1:
        return summaries[0]
    joined = "\n".join(f"- {s}" for s in summaries)
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=TEXT_MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": (
                "Below are per-section summaries of one government meeting's minutes. "
                "Write a single 2-3 sentence plain-English summary for a citizen who "
                "hasn't read the minutes, leading with the most consequential outcomes "
                "(what passed, failed, was tabled). Return only the summary.\n\n" + joined
            )}],
        )
        record_anthropic(TEXT_MODEL, resp.usage)
        for block in resp.content:
            if getattr(block, "type", "") == "text" and block.text.strip():
                return block.text.strip()
    except Exception:
        pass
    return max(summaries, key=len)
