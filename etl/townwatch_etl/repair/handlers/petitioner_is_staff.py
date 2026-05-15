"""
Repair handler: petitioner_name contains a staff title → re-extract just
the petitioner field from the source minutes via focused vision.

The qa_petitioner_is_staff detector flags motions where the petitioner
field captured a staff member (Director, Administrator, Attorney, etc.).
Most of these are model confusion between "who recommended" and "who
applied". A few are legitimate (e.g., "Mike White, Tint Shop owner
(represented by attorney Jim Trotter)" — Mike is the petitioner, attorney
is just context).

Strategy: hit the meeting PDF with a focused vision prompt that asks ONE
question — "for this specific motion, who is the actual applicant/petitioner,
distinct from the staff recommender?" The model is forced to either name
a non-staff entity or return null. We then update motion.petitioner_name
in place.
"""

from __future__ import annotations

import base64
import json
import urllib.request

import anthropic
import psycopg

from ...config import ANTHROPIC_API_KEY
from ..pdf_utils import trim_pdf
from .base import RepairHandler, RepairOutcome, RepairResult


VISION_MODEL = "claude-sonnet-4-6"

PROMPT_TEMPLATE = """\
You are reviewing one specific motion from the attached meeting minutes PDF.

Motion title: {title}
Motion type:  {motion_type}
Currently captured petitioner: {current_petitioner}

The current value looks like it may be the STAFF RECOMMENDER (e.g., a
director, administrator, attorney, clerk), not the actual PETITIONER
(the applicant, developer, business, or resident who requested the action).

Answer ONE question by reading the minutes:

Who is the actual petitioner for this motion — the entity that filed,
applied, or requested the action that the council voted on? This is
distinct from any city staff member who summarized or recommended it.

Return JSON in this exact shape, nothing else:

{{
  "petitioner_name": "<the actual applicant, or null if no external petitioner exists>",
  "is_internal_initiative": true | false,
  "confidence": "high" | "medium" | "low",
  "reasoning": "<one sentence explaining your answer>"
}}

If the action was initiated by city staff with no external petitioner
(e.g., a city-employee COLA, a millage rate adoption, a city-driven
ordinance), set petitioner_name to null and is_internal_initiative=true."""


class PetitionerIsStaffHandler(RepairHandler):
    handler_id = "petitioner_reextract"

    def can_handle(self, finding: dict, motion: dict) -> bool:
        return finding.get("pattern_id") == "qa_petitioner_is_staff"

    def repair(self, conn: psycopg.Connection, finding: dict, motion: dict) -> RepairResult:
        motion_id = motion["id"]
        title = motion["title"]
        motion_type = motion.get("motion_type") or "unknown"

        # Get the meeting's minutes URL
        meeting = conn.execute(
            "SELECT minutes_url FROM meeting WHERE id = %s",
            (motion["meeting_id"],),
        ).fetchone()
        if not meeting or not meeting["minutes_url"]:
            return RepairResult(
                outcome=RepairOutcome.SKIPPED,
                handler=self.handler_id,
                notes="No minutes_url on parent meeting; cannot re-extract.",
            )

        try:
            pdf_bytes = _download_pdf(meeting["minutes_url"])
        except Exception as e:
            return RepairResult(
                outcome=RepairOutcome.ERROR,
                handler=self.handler_id,
                notes=f"PDF download failed: {e}",
            )

        # Send only the pages containing this motion (5-10x cheaper input)
        pdf_bytes, pdf_note = trim_pdf(pdf_bytes, title)

        prompt = PROMPT_TEMPLATE.format(
            title=title,
            motion_type=motion_type,
            current_petitioner=motion.get("petitioner_name") or "(unknown)",
        )

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=VISION_MODEL,
            max_tokens=1024,
            output_config={"effort": "low"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": base64.standard_b64encode(pdf_bytes).decode("utf-8"),
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                },
            ],
        )

        try:
            payload = _parse_json(response)
        except ValueError as e:
            return RepairResult(
                outcome=RepairOutcome.ERROR,
                handler=self.handler_id,
                notes=f"Model returned non-JSON: {e}",
            )

        new_petitioner = payload.get("petitioner_name")
        is_internal = bool(payload.get("is_internal_initiative"))
        confidence = payload.get("confidence", "low")

        # If model is low-confidence and there's still a staff-looking name, refuse
        if confidence == "low":
            return RepairResult(
                outcome=RepairOutcome.UNREPAIRABLE,
                handler=self.handler_id,
                notes=f"Model returned low confidence: {payload.get('reasoning','')}",
            )

        conn.execute(
            "UPDATE motion SET petitioner_name = %s WHERE id = %s",
            (new_petitioner, motion_id),
        )
        return RepairResult(
            outcome=RepairOutcome.REPAIRED,
            handler=self.handler_id,
            notes=(
                f"petitioner_name → {new_petitioner!r} "
                f"(internal={is_internal}, conf={confidence}, scanned={pdf_note}): "
                f"{payload.get('reasoning','')}"
            ),
            mutations={
                "field": "petitioner_name",
                "old": motion.get("petitioner_name"),
                "new": new_petitioner,
                "pdf_scope": pdf_note,
            },
        )


def _download_pdf(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "TownWatch/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _parse_json(response) -> dict:
    import re
    text = ""
    for block in response.content:
        if getattr(block, "type", "") == "text":
            text = block.text
            break
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last == -1:
        raise ValueError(f"no JSON object in: {text[:200]!r}")
    return json.loads(text[first : last + 1])
