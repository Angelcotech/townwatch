"""
Pattern: unanimity_rate

Detects governing bodies where a high percentage of motions pass unanimously.
Rubber-stamp signal — when a deliberative body votes N-0 the vast majority of
the time, the real decisions are happening before the public vote.

Severity:
  - 3 if unanimity rate >= 95% and total motions >= 100
  - 2 if rate >= 90% and total >= 50
  - 1 if rate >= 80% and total >= 30
"""

from __future__ import annotations

import psycopg

from .base import Finding, Pattern


class UnanimityRate(Pattern):
    pattern_id = "unanimity_rate"

    def detect(self, conn: psycopg.Connection) -> list[Finding]:
        rows = conn.execute("""
            SELECT
                gb.id          AS body_id,
                gb.name        AS body_name,
                j.id           AS jurisdiction_id,
                j.display_name AS jurisdiction_name,
                COUNT(*)       AS total,
                SUM(CASE
                    WHEN m.vote_tally_yes > 0
                     AND m.vote_tally_no = 0
                     AND m.vote_tally_abstain = 0
                    THEN 1 ELSE 0 END) AS unanimous_yes,
                SUM(CASE WHEN m.vote_tally_no > 0 THEN 1 ELSE 0 END) AS had_dissent
            FROM motion m
            JOIN meeting mtg ON mtg.id = m.meeting_id
            JOIN governing_body gb ON gb.id = mtg.governing_body_id
            JOIN jurisdiction j ON j.id = gb.jurisdiction_id
            WHERE m.vote_tally_yes + m.vote_tally_no + m.vote_tally_abstain > 0
            GROUP BY gb.id, gb.name, j.id, j.display_name
            HAVING COUNT(*) >= 30
        """).fetchall()

        findings: list[Finding] = []
        for r in rows:
            total = int(r["total"])
            unanimous = int(r["unanimous_yes"])
            dissents = int(r["had_dissent"])
            rate_pct = unanimous * 100 // total

            severity = 0
            if rate_pct >= 95 and total >= 100:
                severity = 3
            elif rate_pct >= 90 and total >= 50:
                severity = 2
            elif rate_pct >= 80:
                severity = 1
            if severity == 0:
                continue

            title = (
                f"{r['jurisdiction_name']} {r['body_name']} votes unanimously "
                f"{rate_pct}% of the time ({unanimous} of {total} motions with recorded tallies)."
            )
            explanation = (
                "When a deliberative body votes N-0 the vast majority of the time, "
                "the real decisions are typically being made before the public vote — "
                "in committee, executive session, or informal coordination among members. "
                "This is the structural signature of rubber-stamp governance, where the "
                "public meeting is a ratification ceremony rather than a deliberation."
            )

            findings.append(Finding(
                pattern_id=self.pattern_id,
                severity=severity,
                title=title,
                explanation=explanation,
                jurisdiction_id=int(r["jurisdiction_id"]),
                governing_body_id=int(r["body_id"]),
                metrics={
                    "total_motions": total,
                    "unanimous_yes": unanimous,
                    "had_any_dissent": dissents,
                    "unanimity_rate_pct": rate_pct,
                },
            ))
        return findings
