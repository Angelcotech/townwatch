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
from ..patterns.reconsidered_motion import ReconsideredMotion
from ..patterns.recusal_absence import RecusalAbsence
from ..patterns.unanimity_rate import UnanimityRate


ALL_PATTERNS = [
    UnanimityRate(),
    RecusalAbsence(),
    ReconsideredMotion(),
    DevelopmentBundle(),
]


class RunPatterns(IngestJob):
    source_name = "townwatch_patterns"
    source_type = "manual"
    source_url = "internal://patterns"

    def ingest(self) -> None:
        assert self.conn is not None and self.data_source_id is not None

        for pat in ALL_PATTERNS:
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


def main() -> int:
    result = RunPatterns().run()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
