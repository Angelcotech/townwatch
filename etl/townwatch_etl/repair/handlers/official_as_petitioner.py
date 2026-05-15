"""
Repair handler: petitioner_name names an elected official on the same body.

The petitioner field captured a council member as if they had filed the
motion externally. Almost always a model confusion between "movant"
(who made the motion in the meeting) and "petitioner" (who filed the
underlying request, if external).

Repair: set petitioner_name to NULL. The council role they actually
played is recorded separately in motion.movant during extraction.
Deterministic, no API calls.
"""

from __future__ import annotations

import psycopg

from .base import RepairHandler, RepairOutcome, RepairResult


class OfficialAsPetitionerHandler(RepairHandler):
    handler_id = "official_as_petitioner_clear"

    def can_handle(self, finding: dict, motion: dict) -> bool:
        return finding.get("pattern_id") == "qa_official_as_petitioner"

    def repair(self, conn: psycopg.Connection, finding: dict, motion: dict) -> RepairResult:
        motion_id = motion["id"]
        old = motion.get("petitioner_name")
        conn.execute(
            "UPDATE motion SET petitioner_name = NULL WHERE id = %s",
            (motion_id,),
        )
        return RepairResult(
            outcome=RepairOutcome.REPAIRED,
            handler=self.handler_id,
            notes=(
                f"Cleared petitioner_name (was {old!r}) — was an elected official, "
                "not an external petitioner."
            ),
            mutations={"field": "petitioner_name", "old": old, "new": None},
        )
