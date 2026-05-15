"""
Repair handler: declared vote tally != actual vote rows → re-extract the
vote section for this specific motion from the source PDF.

Covers both shapes:
  - declared=N actual=N-1 (a vote name didn't resolve to an official)
  - declared=N actual=N+1 (a name was duplicated, or hallucinated)

Strategy: hit the PDF with a focused vision prompt that asks for the
EXACT vote roll for ONE specific motion (identified by title). The model
returns a list of {name, vote, notes}. We then:
  1. Identity-resolve each name to an official (CachedResolver)
  2. Replace ALL existing vote rows for this motion atomically
  3. Update the declared tally to match if needed

The voice_vote handler runs FIRST for declared=1 actual=0 cases (which
this handler would otherwise also catch), so by the time we get here we
know this is a real motion that just has bad vote data.
"""

from __future__ import annotations

import base64
import json
import urllib.request

import anthropic
import psycopg

from ...config import ANTHROPIC_API_KEY
from ...identity import CachedResolver
from ..pdf_utils import trim_pdf
from .base import RepairHandler, RepairOutcome, RepairResult


VISION_MODEL = "claude-sonnet-4-6"

PROMPT_TEMPLATE = """\
You are reviewing one specific motion from the attached meeting minutes PDF.

Meeting date: {meeting_date}
Motion title: {title}

Our current extraction has a vote-count integrity issue:
  - declared tally: yes={declared_yes} no={declared_no} abstain={declared_abstain} absent={declared_absent}
  - actual vote rows captured: {actual_votes}

The numbers don't match. Re-read the minutes carefully for this specific
motion and give us the authoritative vote record.

Find this exact motion in the PDF. List every individual vote recorded
for it, exactly as written. If a member is recorded as recused due to
conflict, use "conflict_recusal". If a member was absent and did not
vote, use "absent".

Return JSON in this exact shape, nothing else:

{{
  "found": true | false,
  "motion_location": "<page number or section reference>",
  "votes": [
    {{"name": "<exact name as written>", "vote": "yes|no|abstain|absent|conflict_recusal", "notes": "<optional context>"}},
    ...
  ],
  "tally": {{"yes": N, "no": N, "abstain": N, "absent": N}},
  "confidence": "high" | "medium" | "low",
  "reasoning": "<one sentence on what was wrong before>"
}}

If you cannot find this motion in the document, set found=false and
return an empty votes list."""


class VoteMismatchHandler(RepairHandler):
    handler_id = "vote_reextract"

    def can_handle(self, finding: dict, motion: dict) -> bool:
        if finding.get("pattern_id") != "qa_tally_mismatch":
            return False
        metrics = finding.get("metrics") or {}
        declared = metrics.get("declared_tally") or 0
        actual = metrics.get("actual_votes") or 0
        # Voice vote handler (declared=1, actual=0) and bundled tally
        # handler claim their own cases first.
        if declared == 1 and actual == 0:
            return False
        if declared >= actual + 5 and motion.get("motion_type") == "appointment":
            return False
        return declared != actual

    def repair(self, conn: psycopg.Connection, finding: dict, motion: dict) -> RepairResult:
        motion_id = motion["id"]
        meeting = conn.execute(
            "SELECT id, meeting_date, minutes_url, governing_body_id FROM meeting WHERE id = %s",
            (motion["meeting_id"],),
        ).fetchone()
        if not meeting or not meeting["minutes_url"]:
            return RepairResult(
                outcome=RepairOutcome.SKIPPED,
                handler=self.handler_id,
                notes="No minutes_url on parent meeting; cannot re-extract.",
            )

        # Get jurisdiction_id for the CachedResolver
        body = conn.execute(
            "SELECT jurisdiction_id FROM governing_body WHERE id = %s",
            (meeting["governing_body_id"],),
        ).fetchone()
        if not body:
            return RepairResult(
                outcome=RepairOutcome.SKIPPED,
                handler=self.handler_id,
                notes="Could not resolve jurisdiction for this motion.",
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
        pdf_bytes, pdf_note = trim_pdf(pdf_bytes, motion["title"])

        prompt = PROMPT_TEMPLATE.format(
            meeting_date=meeting["meeting_date"],
            title=motion["title"],
            declared_yes=motion.get("vote_tally_yes") or 0,
            declared_no=motion.get("vote_tally_no") or 0,
            declared_abstain=motion.get("vote_tally_abstain") or 0,
            declared_absent=motion.get("vote_tally_absent") or 0,
            actual_votes=(finding.get("metrics") or {}).get("actual_votes", 0),
        )

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=VISION_MODEL,
            max_tokens=2048,
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

        if not payload.get("found"):
            return RepairResult(
                outcome=RepairOutcome.UNREPAIRABLE,
                handler=self.handler_id,
                notes=f"Model could not locate this motion in the PDF: {payload.get('reasoning','')}",
            )

        if payload.get("confidence") == "low":
            return RepairResult(
                outcome=RepairOutcome.UNREPAIRABLE,
                handler=self.handler_id,
                notes=f"Model returned low confidence: {payload.get('reasoning','')}",
            )

        votes = payload.get("votes") or []
        tally = payload.get("tally") or {}

        # Resolve every vote name to an official_id. Dedupe by official_id —
        # model occasionally returns the same person twice (once named, once
        # implied by "all in favor"), which would violate the (official_id,
        # motion_id) unique constraint on insert.
        resolver = CachedResolver(
            conn,
            jurisdiction_id=body["jurisdiction_id"],
            data_source_id=None,
            source_system="repair_engine",
        )
        seen_oids: set[int] = set()
        resolved_votes = []
        unresolved = []
        duplicates_dropped = 0
        for v in votes:
            name = (v.get("name") or "").strip()
            if not name:
                continue
            oid = resolver.resolve(name)
            if oid is None:
                unresolved.append(name)
                continue
            if oid in seen_oids:
                duplicates_dropped += 1
                continue
            seen_oids.add(oid)
            resolved_votes.append((oid, v.get("vote"), v.get("notes")))

        if unresolved:
            return RepairResult(
                outcome=RepairOutcome.UNREPAIRABLE,
                handler=self.handler_id,
                notes=f"Could not resolve {len(unresolved)} vote name(s): {unresolved}",
            )

        # Recompute the tally from the deduped vote rows, ignoring the
        # model's `tally` field. The granular per-name votes are ground
        # truth; the model's tally summary is frequently inconsistent with
        # its own row list (especially around 'absent').
        recomputed_tally = {"yes": 0, "no": 0, "abstain": 0, "absent": 0}
        for _, value, _ in resolved_votes:
            if value in recomputed_tally:
                recomputed_tally[value] += 1
            elif value == "conflict_recusal":
                # Recusals don't count toward the canonical tally fields;
                # they're tracked via vote.vote_value only.
                pass
        tally = recomputed_tally

        # Replace vote rows atomically. data_source_id is piped through
        # by the engine; falls back to the first vote's original source.
        data_source_id = motion.get("_repair_data_source_id")
        if data_source_id is None:
            existing = conn.execute(
                "SELECT data_source_id FROM vote WHERE motion_id = %s LIMIT 1", (motion_id,)
            ).fetchone()
            data_source_id = existing["data_source_id"] if existing else None
        if data_source_id is None:
            return RepairResult(
                outcome=RepairOutcome.ERROR,
                handler=self.handler_id,
                notes="No data_source_id available; cannot insert new vote rows.",
            )

        conn.execute("DELETE FROM vote WHERE motion_id = %s", (motion_id,))
        for oid, value, notes in resolved_votes:
            conn.execute(
                "INSERT INTO vote (motion_id, official_id, vote_value, notes, data_source_id) VALUES (%s, %s, %s, %s, %s)",
                (motion_id, oid, value, notes, data_source_id),
            )

        # Update the motion's declared tally to match the re-extracted truth
        conn.execute(
            """UPDATE motion
               SET vote_tally_yes = %s, vote_tally_no = %s,
                   vote_tally_abstain = %s, vote_tally_absent = %s
               WHERE id = %s""",
            (
                int(tally.get("yes", 0)),
                int(tally.get("no", 0)),
                int(tally.get("abstain", 0)),
                int(tally.get("absent", 0)),
                motion_id,
            ),
        )

        dup_note = f" (dropped {duplicates_dropped} duplicate name{'s' if duplicates_dropped != 1 else ''})" if duplicates_dropped else ""
        return RepairResult(
            outcome=RepairOutcome.REPAIRED,
            handler=self.handler_id,
            notes=(
                f"Re-extracted {len(resolved_votes)} votes{dup_note}; tally → "
                f"{tally['yes']}-{tally['no']}-{tally['abstain']}-{tally['absent']} "
                f"(recomputed from rows). {payload.get('reasoning','')}"
            ),
            mutations={
                "votes_replaced": len(resolved_votes),
                "tally": tally,
                "duplicates_dropped": duplicates_dropped,
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
