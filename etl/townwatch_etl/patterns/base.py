"""
Pattern-detection engine — base classes.

Each Pattern is a deterministic detector that scans the database for a
specific corruption / conflict / governance pattern and emits Findings.
A Finding is a sentence-level fact backed by evidence rows from the
existing tables.

The runner (jobs/run_patterns.py) loads every Pattern, calls detect(),
and writes findings to the `finding` table. Re-running replaces findings
for that pattern_id — findings are always derived from current data.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Any

import psycopg


@dataclass
class Finding:
    """One sentence-level pattern detection with evidence + explanation."""
    pattern_id: str
    severity: int                              # 1 suggestive → 5 documented
    title: str                                 # the shareable sentence
    explanation: str | None = None             # plain-English why this is flagged
    jurisdiction_id: int | None = None
    governing_body_id: int | None = None
    subject_official_id: int | None = None
    subject_motion_id: int | None = None
    evidence: list[dict] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


class Pattern(ABC):
    """Subclass per detection rule. Must set pattern_id."""

    pattern_id: str = ""

    @abstractmethod
    def detect(self, conn: psycopg.Connection) -> list[Finding]:
        ...


def write_findings(
    conn: psycopg.Connection,
    findings: list[Finding],
    *,
    pattern_id: str,
    data_source_id: int | None = None,
) -> int:
    """Replace all findings for this pattern_id with the new list. Idempotent."""
    conn.execute("DELETE FROM finding WHERE pattern_id = %s", (pattern_id,))
    if not findings:
        return 0
    rows_written = 0
    for f in findings:
        conn.execute(
            """
            INSERT INTO finding
                (pattern_id, severity, title, explanation,
                 jurisdiction_id, governing_body_id,
                 subject_official_id, subject_motion_id,
                 evidence, metrics, data_source_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
            """,
            (
                f.pattern_id,
                f.severity,
                f.title,
                f.explanation,
                f.jurisdiction_id,
                f.governing_body_id,
                f.subject_official_id,
                f.subject_motion_id,
                json.dumps(f.evidence),
                json.dumps(f.metrics),
                data_source_id,
            ),
        )
        rows_written += 1
    return rows_written
