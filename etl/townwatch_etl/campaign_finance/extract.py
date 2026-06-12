"""
CCDR extraction — itemized contributions from Georgia Campaign Contribution
Disclosure Reports.

Input is the recovered TEXT of a CCDR (the document_text store handles the
scan→text problem: text layer when present, Mistral OCR otherwise — most
local CCDRs are image scans). CCDRs are short state forms (≤ ~15 pages), so
one text window suffices; no recovery ladder needed.

Two shapes matter:
  * itemized reports — contribution tables (name, address, employer,
    occupation, date, amount) plus declared summary totals;
  * minimal-activity reports — candidates who filed the $2,500-exemption
    affidavit file CCDRs with empty schedules; contributions=[] with the
    declared totals (often zero) is the CORRECT extraction, not a failure.
"""

from __future__ import annotations

from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from ..config import ANTHROPIC_API_KEY
from ..llm_client import record_anthropic

TEXT_MODEL = "claude-haiku-4-5"
EXTRACTOR_VERSION = f"ccdr-v1:{TEXT_MODEL}"


class CCDRContribution(BaseModel):
    contributor_name: str
    contributor_type: Literal["individual", "business", "pac", "other"] | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    employer: str | None = None
    occupation: str | None = None
    amount: float
    date: str | None = Field(None, description="ISO date YYYY-MM-DD when legible")


class CCDRExtraction(BaseModel):
    candidate_name: str | None = None
    office: str | None = None
    election_year: int | None = None
    filing_period_start: str | None = Field(None, description="ISO date")
    filing_period_end: str | None = Field(None, description="ISO date")
    total_contributions: float | None = None
    total_expenditures: float | None = None
    cash_on_hand: float | None = None
    contributions: list[CCDRContribution] = []
    extraction_confidence: Literal["high", "medium", "low"] = "medium"


_INSTRUCTIONS = """\
You are reading the text of a Georgia Campaign Contribution Disclosure Report
(CCDR) — a state form filed by a local candidate. Extract exactly what the
form says into JSON matching this schema (output BARE JSON, nothing else):

{schema}

Rules:
- Itemize every contribution row from the contribution schedules. Do NOT
  invent rows: a report with empty schedules (common for candidates who filed
  the $2,500 exemption affidavit) correctly has "contributions": [].
- Use null for anything illegible or absent — never guess names or amounts.
- Amounts are dollars as numbers (no $ signs, no commas).
- The summary page's declared totals go in total_contributions /
  total_expenditures / cash_on_hand when present.
- extraction_confidence: "high" only when the text is clean and complete;
  "low" when OCR damage makes amounts or names uncertain.
"""


def extract_ccdr_text(text: str) -> CCDRExtraction:
    """One text window over a recovered CCDR (Haiku → JSON)."""
    import json as _json
    import re as _re

    prompt = _INSTRUCTIONS.format(schema=_json.dumps(CCDRExtraction.model_json_schema(), indent=1))
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    with client.messages.stream(
        model=TEXT_MODEL,
        max_tokens=16000,
        messages=[{"role": "user", "content": prompt + "\n--- DOCUMENT TEXT ---\n" + text}],
    ) as stream:
        response = stream.get_final_message()
    record_anthropic(TEXT_MODEL, response.usage)

    out = ""
    for block in response.content:
        if getattr(block, "type", "") == "text":
            out = block.text
            break
    fence = _re.search(r"```(?:json)?\s*(\{.*\})\s*```", out, _re.DOTALL)
    if fence:
        out = fence.group(1)
    first, last = out.find("{"), out.rfind("}")
    if first == -1 or last < first:
        stop = getattr(response, "stop_reason", "?")
        raise ValueError(f"No JSON in CCDR extraction response (stop_reason={stop}): {out[:200]!r}")
    return CCDRExtraction.model_validate(_json.loads(out[first:last + 1]))
