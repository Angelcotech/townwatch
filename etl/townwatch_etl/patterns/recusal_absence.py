"""
Pattern: recusal_absence

Detects officials with substantial vote records and zero recusals. Recusals
are how officials themselves disclose conflicts of interest. An official
with hundreds of votes and zero recusals over many years has either had
zero financial conflicts (rare for property-owning adults), or has not
disclosed the ones they had.

Severity:
  - 3 if >300 votes, 0 recusals, 5+ years record
  - 2 if >100 votes, 0 recusals
  - 1 if >50 votes, 0 recusals
"""

from __future__ import annotations

import psycopg

from .base import Finding, Pattern


class RecusalAbsence(Pattern):
    pattern_id = "recusal_absence"

    def detect(self, conn: psycopg.Connection) -> list[Finding]:
        rows = conn.execute("""
            SELECT
                o.id           AS official_id,
                o.canonical_name,
                COUNT(*)       AS total_votes,
                SUM(CASE WHEN v.vote_value = 'conflict_recusal' THEN 1 ELSE 0 END) AS recusals,
                MIN(mtg.meeting_date) AS first_vote,
                MAX(mtg.meeting_date) AS last_vote,
                MIN(j.id)      AS jurisdiction_id
            FROM vote v
            JOIN official o   ON o.id = v.official_id
            JOIN motion m     ON m.id = v.motion_id
            JOIN meeting mtg  ON mtg.id = m.meeting_id
            JOIN governing_body gb ON gb.id = mtg.governing_body_id
            JOIN jurisdiction j ON j.id = gb.jurisdiction_id
            GROUP BY o.id, o.canonical_name
            HAVING SUM(CASE WHEN v.vote_value = 'conflict_recusal' THEN 1 ELSE 0 END) = 0
               AND COUNT(*) > 50
        """).fetchall()

        findings: list[Finding] = []
        for r in rows:
            total = int(r["total_votes"])
            years = round((r["last_vote"] - r["first_vote"]).days / 365.25, 1)

            severity = 0
            if total > 300 and years >= 5:
                severity = 3
            elif total > 100:
                severity = 2
            else:
                severity = 1

            title = (
                f"{r['canonical_name']} has cast {total:,} votes across "
                f"{years} years with zero recorded recusals."
            )
            explanation = (
                "Recusals are how officials themselves disclose conflicts of interest. "
                "An official with substantial vote history and zero recusals has either "
                "had no financial conflicts arise (unusual for property-owning adults "
                "over multi-year tenure), or has not declared the conflicts they did have. "
                "The absence is a structural signal, not proof — but it warrants reviewing "
                "their votes against their known property and business interests."
            )

            findings.append(Finding(
                pattern_id=self.pattern_id,
                severity=severity,
                title=title,
                explanation=explanation,
                jurisdiction_id=int(r["jurisdiction_id"]),
                subject_official_id=int(r["official_id"]),
                metrics={
                    "total_votes": total,
                    "years_of_record": years,
                    "first_vote": str(r["first_vote"]),
                    "last_vote": str(r["last_vote"]),
                    "recusals": 0,
                },
            ))
        return findings
