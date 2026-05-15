"""
Run all pattern detectors against the current database state.

Each detector returns a list of Finding objects. We persist them to the
`finding` table (one row per finding), replacing prior runs for that
pattern_id atomically.

Run:
    python -m townwatch_etl.jobs.run_patterns
"""

from __future__ import annotations

import json
import sys

from ..db import connect
from ..ingest_base import IngestJob
from ..patterns.base import write_findings
from ..patterns.development_bundle import DevelopmentBundle
from ..patterns.qa_data_anomalies import (
    QaGenericPetitioner,
    QaLowConfidenceMeeting,
    QaOfficialAsPetitioner,
    QaOrphanOfficial,
    QaPetitionerIsStaff,
    QaShortMotionTitle,
    QaTallyMismatch,
)
from ..patterns.qa_official_name_typos import QaOfficialNameTypos
from ..patterns.reconsidered_motion import ReconsideredMotion
from ..patterns.recusal_absence import RecusalAbsence
from ..patterns.unanimity_rate import UnanimityRate


GOVERNANCE_PATTERNS = [
    UnanimityRate(),
    RecusalAbsence(),
    ReconsideredMotion(),
    DevelopmentBundle(),
]

QA_PATTERNS = [
    QaOfficialNameTypos(),
    QaPetitionerIsStaff(),
    QaTallyMismatch(),
    QaShortMotionTitle(),
    QaOrphanOfficial(),
    QaLowConfidenceMeeting(),
    QaGenericPetitioner(),
    QaOfficialAsPetitioner(),
]

ALL_PATTERNS = GOVERNANCE_PATTERNS + QA_PATTERNS


class RunPatterns(IngestJob):
    source_name = "townwatch_patterns"
    source_type = "manual"
    source_url = "internal://patterns"

    def ingest(self) -> None:
        assert self.conn is not None and self.data_source_id is not None

        for group_name, group in (
            ("governance", GOVERNANCE_PATTERNS),
            ("quality assurance", QA_PATTERNS),
        ):
            print(f"\n— {group_name} patterns —")
            for pat in group:
                print(f"  → running pattern: {pat.pattern_id}")
                findings = pat.detect(self.conn)
                n = write_findings(
                    self.conn,
                    findings,
                    pattern_id=pat.pattern_id,
                    data_source_id=self.data_source_id,
                )
                print(f"     wrote {n} finding(s)")
                self.rows_written += n

        print("\n— syncing data_status quarantine —")
        sync_quarantine(self.conn)


def sync_quarantine(conn) -> None:
    """
    Reconcile motion.data_status with the current set of QA findings.

    - Any motion that is the subject of an unresolved 'qa_*' finding → 'disputed'.
    - Any motion previously 'disputed' but no longer flagged → back to 'clean'.
    - 'repairing' status is owned by the repair_engine and not touched here.

    This is the protocol substrate. Quarantine is a function of current findings,
    never a manual decision.
    """
    # 1. Mark every motion with a current QA finding as 'disputed'
    flagged = conn.execute("""
        UPDATE motion m
        SET data_status        = 'disputed',
            data_status_reason = sub.pattern_id,
            data_status_at     = NOW()
        FROM (
            SELECT DISTINCT ON (subject_motion_id)
                   subject_motion_id, pattern_id
            FROM finding
            WHERE pattern_id LIKE 'qa_%'
              AND subject_motion_id IS NOT NULL
            ORDER BY subject_motion_id, severity DESC
        ) sub
        WHERE m.id = sub.subject_motion_id
          AND m.data_status != 'repairing'
        RETURNING m.id
    """).fetchall()

    # 2. Clear motions that were 'disputed' but no longer have any QA finding
    cleared = conn.execute("""
        UPDATE motion
        SET data_status        = 'clean',
            data_status_reason = NULL,
            data_status_at     = NOW()
        WHERE data_status = 'disputed'
          AND id NOT IN (
              SELECT subject_motion_id FROM finding
              WHERE pattern_id LIKE 'qa_%' AND subject_motion_id IS NOT NULL
          )
        RETURNING id
    """).fetchall()

    print(f"  · {len(flagged)} motion(s) quarantined")
    print(f"  · {len(cleared)} motion(s) cleared back to clean")


def main() -> int:
    result = RunPatterns().run()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
