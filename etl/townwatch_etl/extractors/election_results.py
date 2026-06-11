"""
Extract structured election results from a certified-results document.

Input: a PDF (most common) or screenshot of a Board-of-Elections certified
results document. Could be a per-election summary listing all contests, or
a single-contest detail page.

Output: ElectionResultsExtraction with one Contest entry per race, each
carrying candidate names + vote counts + winner flag. The ingest job
(jobs/ingest_elections.py) then resolves contests to governing_body +
seat records and updates term start/end dates.

This is intentionally a pure extractor — it does NOT decide what to do
with the data, only what's printed on the page. Identity resolution,
term-transition logic, and DB writes happen downstream.

Generic across jurisdictions and states: same shape as agendas.py /
council_roster.py. The vision prompt itself is platform-agnostic; the
upstream ingestor wraps it with platform-specific fetch logic.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel, Field

from ..config import ANTHROPIC_API_KEY


Confidence = Literal["high", "medium", "low"]


class CandidateResult(BaseModel):
    name: str = Field(description="Candidate's full name as printed.")
    party: Optional[str] = Field(
        default=None,
        description="Party affiliation as printed (e.g. 'Republican', 'Democrat', 'Nonpartisan'). Null if not shown — many local races are nonpartisan.",
    )
    votes: Optional[int] = Field(
        default=None,
        description="Vote count for this candidate. Null if not shown.",
    )
    is_winner: bool = Field(
        default=False,
        description="True if the document indicates this candidate won the contest. Look for explicit winner markings (asterisks, bold, 'WINNER' label) or — if the document is a summary — the candidate with the highest votes in a contest with only one seat available.",
    )


class Contest(BaseModel):
    contest_name: str = Field(
        description="Full contest name as printed on the document, e.g. 'Columbia County Board of Commissioners, District 3' or 'Grovetown City Council, Seat 2'."
    )
    body_hint: Optional[str] = Field(
        default=None,
        description="Best guess at which governing body the seat belongs to, e.g. 'Board of Commissioners', 'City Council', 'Board of Education'. Used by the ingest job to JOIN to the right governing_body row.",
    )
    seat_hint: Optional[str] = Field(
        default=None,
        description="Best guess at the seat identifier within the body — usually a district number, ward, or seat name. e.g. 'District 3', 'Ward 2', 'At-Large'. Null for single-seat bodies.",
    )
    election_date: Optional[str] = Field(
        default=None,
        description="Date of the election (YYYY-MM-DD) if shown on the document. Often the overall document carries this, not per-contest.",
    )
    election_type: Optional[Literal["general", "runoff", "special", "primary", "other"]] = Field(
        default=None,
        description="Type of election if discernible from the document title or content.",
    )
    candidates: list[CandidateResult]


class ElectionResultsExtraction(BaseModel):
    election_date: Optional[str] = Field(
        default=None,
        description="Date of the election the document reports on (YYYY-MM-DD), pulled from the document header if present.",
    )
    election_type: Optional[Literal["general", "runoff", "special", "primary", "other"]] = Field(
        default=None,
        description="Type of election the document covers as a whole.",
    )
    election_jurisdiction: Optional[str] = Field(
        default=None,
        description="Name of the jurisdiction whose Board of Elections published these results (e.g. 'Columbia County, Georgia').",
    )
    contests: list[Contest]
    extraction_confidence: Confidence
    extraction_notes: str = Field(
        default="",
        description="Anything ambiguous: unclear winner markings, partial vote counts, unofficial vs certified status, page layout that made extraction difficult.",
    )


# =====================================================================
# Prompts
# =====================================================================

_RULES = """\
WHAT TO EXTRACT

This document is an election-results summary published by a state, county,
or municipal Board of Elections. For every contest (race) on the document:

  - contest_name: copy exactly as printed
  - body_hint:    best guess at which governing body the seat belongs to
                  (e.g. "Board of Commissioners", "City Council",
                  "Board of Education"). Use the body the seat seats people
                  TO, not the body that runs the election.
  - seat_hint:    district number / ward / seat name if applicable
  - election_date: YYYY-MM-DD (if printed; usually on the document header)
  - election_type: general / runoff / special / primary / other
  - candidates: each candidate's name, party (if shown), votes, and
                whether they won

RULES

1. Winner detection. Look for explicit markers — asterisks, "WINNER",
   "ELECTED", bold formatting, or check marks. When no explicit marker
   exists and there's only one seat available in the contest, the
   highest vote-getter is the winner. When there's a runoff or the
   results are uncertified, set is_winner=false for all candidates and
   note this in extraction_notes.

2. Vote counts. Use the FINAL or CERTIFIED column when multiple columns
   appear. Ignore early-voting / mail-in / election-day breakdowns
   unless they're the only column shown.

3. Names exactly as printed. Don't normalize "JOHN SMITH" to "John Smith"
   — identity resolution happens downstream and benefits from the
   original capitalization.

4. Contest_name should include the jurisdiction qualifier when the
   document covers multi-jurisdiction races (e.g. "Augusta-Richmond
   Commission, District 2" rather than just "District 2"), so the
   ingest job can disambiguate.

5. Partisan vs nonpartisan. If the document doesn't show a party for
   a candidate, leave party null. Many local races are nonpartisan.

6. extraction_confidence: "high" if every contest's candidates and
   votes were clearly readable and a winner was unambiguous. "medium"
   if some fields were unclear or implied. "low" if the document was
   partial, illegible, or appeared to be unofficial / preliminary.
"""


OUTPUT_FORMAT_RULES = """\
Return exactly one JSON object matching the schema below. Do not include
any prose, markdown fences, or text outside the JSON. The response should
BEGIN with `{` and END with `}`. Use null for unknown fields. Use empty
arrays for empty lists.
"""


def _schema_for_prompt() -> str:
    import json as _json
    return _json.dumps(ElectionResultsExtraction.model_json_schema(), indent=2)


VISION_INSTRUCTIONS = f"""\
You are extracting structured election-results data from a Board-of-Elections
certified-results document.

{_RULES}

{OUTPUT_FORMAT_RULES}

JSON SCHEMA:
{_schema_for_prompt()}

Now extract from the attached document.
"""


HTML_INSTRUCTIONS = f"""\
You are extracting structured election-results data from the HTML below.
It comes from a Board-of-Elections results portal.

{_RULES}

{OUTPUT_FORMAT_RULES}

JSON SCHEMA:
{_schema_for_prompt()}

PAGE HTML:
"""


VISION_MODEL = "claude-sonnet-4-6"
HTML_MODEL = "claude-haiku-4-5"


# =====================================================================
# Entry points
# =====================================================================

def extract_from_pdf_bytes(pdf_bytes: bytes) -> ElectionResultsExtraction:
    """Send a PDF to Sonnet vision and return parsed results.

    Note: we deliberately do NOT enable extended thinking here. Election
    results are tabular and require little reasoning to extract; with
    thinking on, the model burns the max_tokens budget on internal
    reasoning and returns an empty text block. Plain extraction with
    a generous max_tokens is more reliable for this content type.

    Streamed with a 32K output budget (same fix as the minutes extractor):
    county-wide official results run dozens of pages, and the old 16384
    non-streaming call either truncated into empty/invalid JSON or tripped
    the SDK's 10-minute non-streaming guard outright."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
    with client.messages.stream(
        model=VISION_MODEL,
        max_tokens=32000,
        messages=[
            {
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
            },
        ],
    ) as stream:
        response = stream.get_final_message()
    return _parse_json_response(response)


def extract_from_html(html: str, *, page_url: str | None = None) -> ElectionResultsExtraction:
    """Send HTML to Haiku and return parsed results. Cheap; works when
    the results page is rendered server-side. Clarity Elections is
    JS-rendered and won't work via this path — use a PDF export instead."""
    instructions = HTML_INSTRUCTIONS
    if page_url:
        instructions = f"Source URL: {page_url}\n\n" + instructions
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=HTML_MODEL,
        max_tokens=8192,
        messages=[{"role": "user", "content": instructions + "\n" + html}],
    )
    return _parse_json_response(response)


def extract_from_screenshot(image_path: Path, *, media_type: str = "image/png") -> ElectionResultsExtraction:
    """For JS-rendered results portals (Clarity Elections, modern county
    sites), screenshot the rendered page and send to vision. Streamed with
    a 32K budget for the same reason as the PDF path."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    img_b64 = base64.standard_b64encode(image_path.read_bytes()).decode("utf-8")
    with client.messages.stream(
        model=VISION_MODEL,
        max_tokens=32000,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": img_b64,
                        },
                    },
                    {"type": "text", "text": VISION_INSTRUCTIONS},
                ],
            },
        ],
    ) as stream:
        response = stream.get_final_message()
    return _parse_json_response(response)


def _parse_json_response(response) -> ElectionResultsExtraction:
    import json as _json
    import re as _re

    text = ""
    for block in response.content:
        if getattr(block, "type", "") == "text":
            text = block.text
            break
    fence = _re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, _re.DOTALL)
    if fence:
        text = fence.group(1)
    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last == -1 or last < first:
        raise ValueError(f"no JSON object in response: {text[:200]!r}")
    return ElectionResultsExtraction.model_validate(_json.loads(text[first : last + 1]))
