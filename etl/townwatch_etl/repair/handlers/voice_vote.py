"""
Repair handler: declared=1, actual=0 → demote to discussion item.

These are not real motions — they're agenda items that got a "yes" tally
of 1 (likely a procedural acknowledgement, voice vote, or consensus item
the extractor over-promoted). The fix is to mark the motion as a
non-voting discussion item: clear its vote tallies and set motion_type
to 'procedural'.

Once demoted, qa_tally_mismatch won't fire for it again (the WHERE clause
requires tally_yes+no+abstain+absent > 0).
"""

from __future__ import annotations

import psycopg

from .base import RepairHandler, RepairOutcome, RepairResult


class VoiceVoteHandler(RepairHandler):
    handler_id = "voice_vote_demote"

    def can_handle(self, finding: dict, motion: dict) -> bool:
        if finding.get("pattern_id") != "qa_tally_mismatch":
            return False
        metrics = finding.get("metrics") or {}
        return metrics.get("declared_tally") == 1 and metrics.get("actual_votes") == 0

    def repair(self, conn: psycopg.Connection, finding: dict, motion: dict) -> RepairResult:
        motion_id = motion["id"]
        conn.execute("""
            UPDATE motion
            SET motion_type        = 'procedural',
                vote_tally_yes     = 0,
                vote_tally_no      = 0,
                vote_tally_abstain = 0,
                vote_tally_absent  = 0,
                outcome            = COALESCE(outcome, 'discussed_no_vote')
            WHERE id = %s
        """, (motion_id,))

        return RepairResult(
            outcome=RepairOutcome.REPAIRED,
            handler=self.handler_id,
            notes="Demoted to procedural; cleared vote tallies (this was a voice/consensus item, not a real motion).",
            mutations={"motion_type": "procedural", "tallies_cleared": True},
        )
