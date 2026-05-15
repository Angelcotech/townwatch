"""
Repair handler: declared tally much larger than actual votes (e.g., 15 vs 5)
where motion_type='appointment' → escalate, do not auto-repair.

This is a single agenda item containing N appointments to N boards, each
voted on individually. The minutes summarize them as one tally line (e.g.,
"5-0 on all appointments" with 3 appointees → declared 15, actual 5).
Splitting them programmatically is risky: we'd need to know which seat
each vote was for, in what order, with what motion text — and the
extracted blob doesn't reliably preserve that.

The right move is to mark this motion for human review and document why
auto-repair refused. Operator can then re-extract the meeting with a
custom prompt or split manually.
"""

from __future__ import annotations

import json

import psycopg

from .base import RepairHandler, RepairOutcome, RepairResult


class BundledTallyHandler(RepairHandler):
    handler_id = "bundled_tally_escalate"

    def can_handle(self, finding: dict, motion: dict) -> bool:
        if finding.get("pattern_id") != "qa_tally_mismatch":
            return False
        metrics = finding.get("metrics") or {}
        declared = metrics.get("declared_tally") or 0
        actual = metrics.get("actual_votes") or 0
        if motion.get("motion_type") != "appointment":
            return False
        return declared >= actual + 5 and declared > actual

    def repair(self, conn: psycopg.Connection, finding: dict, motion: dict) -> RepairResult:
        motion_id = motion["id"]
        existing_meta = motion.get("meta") or {}
        existing_meta["repair_escalation"] = {
            "reason": "bundled_appointment_tally",
            "declared": (finding.get("metrics") or {}).get("declared_tally"),
            "actual": (finding.get("metrics") or {}).get("actual_votes"),
            "action_required": "manual_split_or_reextract",
        }
        conn.execute(
            "UPDATE motion SET meta = %s::jsonb WHERE id = %s",
            (json.dumps(existing_meta), motion_id),
        )
        return RepairResult(
            outcome=RepairOutcome.UNREPAIRABLE,
            handler=self.handler_id,
            notes="Bundled appointment slate — auto-split is unsafe. Flagged for manual review.",
            mutations={"meta.repair_escalation": existing_meta["repair_escalation"]},
        )
