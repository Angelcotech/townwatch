"""
Extract structured roster data from a city/county council web page.

Input: an HTML page (the city's "Mayor and Council" or "Board of
Commissioners" directory) OR a screenshot of it.

Output: list of CouncilMemberRecord with name, photo_url,
term_expires_date, district, title — whichever fields the page actually
shows.

Direct emails are intentionally NOT extracted: most jurisdictions do not
publish them, and we treat citizen-facing contact as a jurisdiction-level
property (City Hall / Commissioners Office) rather than per-official.

Used by jobs/refresh_council_roster.py to fill data the CivicEngage
officials-table scraper doesn't capture (term end dates, phone numbers,
photos). Same content-type-dispatch shape as agendas/minutes/campaign_finance.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from ..config import ANTHROPIC_API_KEY
from ..llm_client import record_anthropic


Confidence = Literal["high", "medium", "low"]


class CouncilMemberRecord(BaseModel):
    name: str = Field(description="Full name as printed on the page, in original casing")
    title: str | None = Field(
        default=None,
        description="Title/role as printed: 'Mayor', 'Mayor Pro Tem', 'Council Member, District 2', etc.",
    )
    district: str | None = Field(
        default=None,
        description="District name/number if the seat is district-based; null for at-large seats.",
    )
    phone: str | None = Field(
        default=None,
        description="Direct phone number if published. Null if not shown.",
    )
    term_expires_date: str | None = Field(
        default=None,
        description="YYYY-MM-DD when the member's current term expires, if shown. "
                    "Often printed as 'Term Expires: December 2026' or 'Re-elected 2024 - 4-year term'. "
                    "Convert to a full ISO date; pick December 31 of the year if only year is shown.",
    )
    term_started_date: str | None = Field(
        default=None,
        description="YYYY-MM-DD when the member's current term began, if shown. Less commonly published.",
    )
    photo_url: str | None = Field(
        default=None,
        description="Absolute URL of the member's photo on the page, if available.",
    )


class CouncilRosterExtraction(BaseModel):
    body_name: str = Field(description="The body the page describes, e.g. 'City Council'")
    members: list[CouncilMemberRecord]
    extraction_confidence: Confidence
    extraction_notes: str = Field(
        default="",
        description="Ambiguities, missing photos, mismatched titles, or signs the page is stale "
                    "(e.g. 'Page header says 2022-2024 term — this data may be outdated').",
    )


# =====================================================================
# Prompts
# =====================================================================

_RULES = """\
WHAT TO EXTRACT

This document is a city's or county's council/commission directory page.
For each member shown, extract:
  - name (exact casing)
  - title (Mayor, Mayor Pro Tem, Council Member District 2, Commissioner, etc.)
  - district (if district-based)
  - direct phone, IF the page publishes it
  - term_expires_date — often stated as "Term Expires: 2026" or
    "Re-elected November 2024, term ends 2028". Convert to YYYY-MM-DD;
    if only a year is given, use December 31 of that year.
  - term_started_date if shown
  - photo_url if a member photo is shown on the page

DO NOT extract email addresses. Direct emails for elected officials are
treated as private and are not modeled. If you see one on the page,
ignore it.

RULES

1. Names: copy exactly as printed. Do not normalize spellings or
   capitalize differently. Identity resolution happens downstream.

2. Term dates: convert any human-readable date format to ISO YYYY-MM-DD.
   "December 2026" → "2026-12-31". "November 2024" → "2024-11-30".
   If a term length and a re-election date are given, derive the end
   date: re-elected November 2024 for a 4-year term → "2028-11-30".
   If you cannot determine a date with confidence, leave it null.

3. District: only populate for district-based seats. For at-large seats
   leave it null.

4. Photo URL: if you see an <img> tag for a member, capture its src as
   an absolute URL (prepend the page's domain if needed). Null if no
   photo is shown.

5. Stale data signal: if the page header indicates a prior term (e.g.
   "2022 City Council" when reading in 2026), call this out in
   extraction_notes.

6. extraction_confidence: "high" if every field for every member was
   clearly readable. "medium" if some fields were unclear or implied.
   "low" if significant content was illegible or the page structure
   was ambiguous.
"""


OUTPUT_FORMAT_RULES = """\
Return exactly one JSON object matching the schema below. Do not include
any prose, markdown fences, or text outside the JSON. The response should
BEGIN with `{` and END with `}`. Use null for unknown/unpublished
fields. Use empty arrays for empty lists.
"""


def _schema_for_prompt() -> str:
    import json as _json
    return _json.dumps(CouncilRosterExtraction.model_json_schema(), indent=2)


HTML_INSTRUCTIONS = f"""\
You are extracting council/commission roster data from the HTML below.

{_RULES}

{OUTPUT_FORMAT_RULES}

JSON SCHEMA:
{_schema_for_prompt()}

PAGE HTML:
"""


VISION_INSTRUCTIONS = f"""\
You are extracting council/commission roster data from a screenshot of
a city's directory page.

Read the page carefully — especially small text under member photos
where term-expires dates are commonly printed.

{_RULES}

{OUTPUT_FORMAT_RULES}

JSON SCHEMA:
{_schema_for_prompt()}

Now extract from the attached screenshot.
"""


HTML_MODEL = "claude-haiku-4-5"
VISION_MODEL = "claude-sonnet-4-6"


# =====================================================================
# Entry points
# =====================================================================

def extract_from_html(html: str, *, page_url: str | None = None) -> CouncilRosterExtraction:
    """Send the HTML directly to Haiku. Cheap; works when the page is
    HTML and not behind JS rendering. Most CivicEngage / CivicPlus
    directories work this way.
    """
    instructions = HTML_INSTRUCTIONS
    if page_url:
        instructions = (
            f"Source URL (for resolving relative image paths): {page_url}\n\n" + instructions
        )
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=HTML_MODEL,
        max_tokens=8192,
        messages=[{"role": "user", "content": instructions + "\n" + html}],
    )
    record_anthropic(HTML_MODEL, response.usage)
    return _parse_json_response(response)


def extract_from_screenshot(image_path: Path, *, media_type: str = "image/png") -> CouncilRosterExtraction:
    """Send a screenshot to Sonnet vision. Used when the page is
    JS-rendered or the HTML is too noisy to extract reliably."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    img_b64 = base64.standard_b64encode(image_path.read_bytes()).decode("utf-8")
    response = client.messages.create(
        model=VISION_MODEL,
        max_tokens=8192,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
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
    )
    record_anthropic(VISION_MODEL, response.usage)
    return _parse_json_response(response)


def _parse_json_response(response) -> CouncilRosterExtraction:
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
    return CouncilRosterExtraction.model_validate(_json.loads(text[first : last + 1]))
