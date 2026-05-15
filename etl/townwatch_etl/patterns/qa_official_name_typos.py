"""
QA Pattern: qa_official_name_typos

Detects officials whose names look like typos of other officials in the
same jurisdiction. Lower similarity threshold than the merge_officials
job so it catches OCR-induced drift even when surname trigrams diverge.

Each finding pairs two officials with similar canonical_names; the
operator decides whether to merge.

Severity:
  - 3 if similarity > 0.7 (very likely a typo)
  - 2 if similarity > 0.5 (probable typo)
  - 1 if similarity > 0.4 AND same first_name
"""

from __future__ import annotations

import psycopg

from .base import Finding, Pattern


class QaOfficialNameTypos(Pattern):
    pattern_id = "qa_official_name_typos"

    def detect(self, conn: psycopg.Connection) -> list[Finding]:
        rows = conn.execute("""
            SELECT
                o1.id AS id1, o1.canonical_name AS n1, o1.first_name AS f1,
                o2.id AS id2, o2.canonical_name AS n2, o2.first_name AS f2,
                similarity(o1.canonical_name, o2.canonical_name) AS sim,
                (SELECT COUNT(*) FROM vote WHERE official_id = o1.id) AS v1,
                (SELECT COUNT(*) FROM vote WHERE official_id = o2.id) AS v2
            FROM official o1
            JOIN official o2 ON o1.id < o2.id
            WHERE similarity(o1.canonical_name, o2.canonical_name) > 0.4
            ORDER BY sim DESC
        """).fetchall()

        findings: list[Finding] = []
        for r in rows:
            sim = float(r["sim"])
            first_match = (r["f1"] or "").lower() == (r["f2"] or "").lower()
            severity = 0
            if sim > 0.7:
                severity = 3
            elif sim > 0.5:
                severity = 2
            elif sim > 0.4 and first_match:
                severity = 1
            if severity == 0:
                continue

            title = (
                f"Possible duplicate officials: '{r['n1']}' ({r['v1']} votes) "
                f"and '{r['n2']}' ({r['v2']} votes) — similarity {sim:.2f}"
                + (" · same first name" if first_match else "")
            )
            explanation = (
                "Two official records have names similar enough to suggest they "
                "may be the same person captured under variant spellings (typically "
                "from OCR errors). Run `merge_officials --force-pair CANONICAL DUP` "
                "to merge if confirmed."
            )
            findings.append(Finding(
                pattern_id=self.pattern_id,
                severity=severity,
                title=title,
                explanation=explanation,
                metrics={
                    "official_id_1": r["id1"],
                    "official_id_2": r["id2"],
                    "name_1": r["n1"],
                    "name_2": r["n2"],
                    "similarity": round(sim, 3),
                    "votes_1": int(r["v1"]),
                    "votes_2": int(r["v2"]),
                    "first_name_match": first_match,
                },
            ))
        return findings
