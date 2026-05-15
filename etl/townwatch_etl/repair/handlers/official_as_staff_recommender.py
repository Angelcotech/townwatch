"""
Repair handler: staff_recommender names an elected official.

Council members don't recommend as staff. The repair nullifies the
bad attribution; the council role (movant, voter) is captured
elsewhere on the motion.
"""

from __future__ import annotations

import psycopg

from .base import RepairHandler, RepairOutcome, RepairResult


class OfficialAsStaffRecommenderHandler(RepairHandler):
    handler_id = "official_as_staff_recommender_clear"

    def can_handle(self, finding: dict, motion: dict) -> bool:
        return finding.get("pattern_id") == "qa_official_as_staff_recommender"

    def repair(self, conn: psycopg.Connection, finding: dict, motion: dict) -> RepairResult:
        motion_id = motion["id"]
        old = motion.get("staff_recommender")
        conn.execute(
            "UPDATE motion SET staff_recommender = NULL WHERE id = %s",
            (motion_id,),
        )
        return RepairResult(
            outcome=RepairOutcome.REPAIRED,
            handler=self.handler_id,
            notes=(
                f"Cleared staff_recommender (was {old!r}) — was an elected official, "
                "not staff."
            ),
            mutations={"field": "staff_recommender", "old": old, "new": None},
        )
