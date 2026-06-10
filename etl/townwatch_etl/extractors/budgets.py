"""
Adopted-budget "translation" — top-line extraction.

A standardized GA budget book (e.g. the UGA/TED filing) runs hundreds of pages,
but the citizen-relevant top line — fiscal-year totals, the by-fund and
by-department breakdown — lives in a small SUMMARY section. Rather than extract
every account line (a later phase), we locate the summary pages by keyword
score, send just those to the model, and return totals + breakdowns + a
plain-language summary.

The model call is wrapped in a schema-validation retry loop: if the output fails
Pydantic validation or is missing both totals, we re-prompt once with the error.
That's a grounded loop (the schema is the external check) — cheap reliability,
not ungrounded "reflect again".
"""

from __future__ import annotations

import io
import json

from pydantic import BaseModel, Field, ValidationError

from ..config import ANTHROPIC_API_KEY
from ..llm_client import record_anthropic

# Financial figures — accuracy matters more than cost, so use the stronger model.
BUDGET_MODEL = "claude-sonnet-4-6"
_MAX_PAGE_CHARS = 3000
_MAX_SUMMARY_PAGES = 22      # summary pages sent to the model
_INTRO_PAGES = 3             # always include the cover/intro

# Pages whose text matches these earn points — the budget summary tables.
_SUMMARY_CUES = [
    "total revenues", "total expenditures", "total revenue", "total expenditure",
    "budget summary", "all funds", "fund summary", "revenues by", "expenditures by",
    "by department", "by function", "by fund", "fund balance", "general fund",
    "summary of", "adopted budget", "appropriation",
]


class FundLine(BaseModel):
    name: str
    revenues: float | None = None
    expenditures: float | None = None


class DeptLine(BaseModel):
    name: str
    amount: float | None = None


class BudgetExtraction(BaseModel):
    fiscal_year: int | None = Field(default=None, description="The budget's fiscal year, e.g. 2026")
    adopted_date: str | None = Field(default=None, description="YYYY-MM-DD if an adoption date is stated")
    total_revenues: float | None = None
    total_expenditures: float | None = None
    funds: list[FundLine] = Field(default_factory=list)
    departments: list[DeptLine] = Field(default_factory=list)
    plain_summary: str = Field(description="2-4 plain-English sentences a resident can understand")
    extraction_confidence: str = Field(default="medium", description="high | medium | low")


def _page_texts(pdf_bytes: bytes) -> list[str]:
    from pypdf import PdfReader
    rd = PdfReader(io.BytesIO(pdf_bytes))
    out = []
    for p in rd.pages:
        out.append(((p.extract_text() or "").strip())[:_MAX_PAGE_CHARS])
    return out


def _select_summary_pages(pages: list[str]) -> list[tuple[int, str]]:
    """Return (absolute_page_index, text) for the intro + highest-scoring
    summary pages, in page order, capped to _MAX_SUMMARY_PAGES."""
    scored = []
    for i, t in enumerate(pages):
        low = t.lower()
        score = sum(low.count(cue) for cue in _SUMMARY_CUES)
        # a page that's mostly numbers + a summary cue is a strong table signal
        if score:
            digits = sum(c.isdigit() for c in t)
            scored.append((i, score + min(digits // 200, 5)))
    scored.sort(key=lambda x: x[1], reverse=True)
    keep = {i for i in range(min(_INTRO_PAGES, len(pages)))}
    for i, _ in scored[: _MAX_SUMMARY_PAGES]:
        keep.add(i)
    return [(i, pages[i]) for i in sorted(keep)]


_PROMPT = """You are translating a local-government ADOPTED BUDGET into the top-line facts a
resident needs. Below are the most relevant pages of the budget book (page
numbers are absolute). Extract:
  - fiscal_year (e.g. 2026)
  - adopted_date (YYYY-MM-DD) only if explicitly stated
  - total_revenues and total_expenditures (whole dollars, all funds combined)
  - funds: the major funds with their revenue/expenditure totals (General Fund,
    enterprise/utility funds, SPLOST, etc.)
  - departments: the largest spending departments/functions with their amounts
  - plain_summary: 2-4 plain-English sentences a resident can understand — what
    this budget funds, its size, and any notable change. No jargon.
  - extraction_confidence: high/medium/low based on how clearly the totals appear.

Use whole-dollar numbers (strip $ and commas). Omit a field rather than guess.
{retry_note}
=== BUDGET PAGES ===
{pages}

Respond with ONLY JSON matching:
{{"fiscal_year": N, "adopted_date": "YYYY-MM-DD"|null, "total_revenues": N|null,
  "total_expenditures": N|null, "funds": [{{"name": "...", "revenues": N|null, "expenditures": N|null}}],
  "departments": [{{"name": "...", "amount": N|null}}], "plain_summary": "...",
  "extraction_confidence": "high|medium|low"}}"""


def _call(prompt: str) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model=BUDGET_MODEL, max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    record_anthropic(BUDGET_MODEL, resp.usage)
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    s, e = text.find("{"), text.rfind("}")
    return json.loads(text[s:e + 1]) if s >= 0 and e > s else {}


def extract_budget(pdf_bytes: bytes) -> tuple[BudgetExtraction, str]:
    """Returns (extraction, method). method notes how many pages were used.
    Raises ValueError if the document yields no usable text."""
    pages = _page_texts(pdf_bytes)
    if not any(pages):
        raise ValueError("budget PDF has no extractable text layer")
    selected = _select_summary_pages(pages)
    block = "\n\n".join(f"[page {i + 1}]\n{t}" for i, t in selected if t)

    retry_note = ""
    last_err = None
    for attempt in (1, 2):
        # Grounded retry: the Pydantic schema (and "totals present") is the
        # external check; on failure we re-prompt once with the error.
        raw = _call(_PROMPT.format(pages=block, retry_note=retry_note))
        try:
            ext = BudgetExtraction.model_validate(raw)
        except ValidationError as ve:
            last_err = ve
            retry_note = (
                f"\nYour previous response failed validation: {str(ve)[:300]}\n"
                "Return valid JSON exactly matching the schema.\n"
            )
            continue
        if ext.total_revenues is None and ext.total_expenditures is None and attempt == 1:
            retry_note = (
                "\nYour previous response had neither total_revenues nor "
                "total_expenditures. Find the all-funds totals and include them.\n"
            )
            last_err = ValueError("no totals")
            continue
        return ext, f"summary_pages={len(selected)}"
    raise ValueError(f"budget extraction failed validation after retry: {last_err}")
