"""
Pattern: reconsidered_motion

Detects motions whose titles are highly similar within a one-year window
in the same governing body — typically the same proposal returning after
modification, postponement, or developer push-back. The Dodge Lane
rezoning is the canonical example: PUD → R-2 → R-2 reconsideration
across 4 months.

Severity:
  - 3 if 3+ versions of the same motion within 12 months
  - 2 if 2 versions within 6 months with same parties involved
  - 1 if 2 highly similar motions within 12 months
"""

from __future__ import annotations

import psycopg

from .base import Finding, Pattern


SIMILARITY_THRESHOLD = 0.55  # pg_trgm threshold; tuned to catch reconsiderations


class ReconsideredMotion(Pattern):
    pattern_id = "reconsidered_motion"

    def detect(self, conn: psycopg.Connection) -> list[Finding]:
        # Find all pairs of motions in the same governing body within 365 days
        # whose titles are highly similar. Group by the cluster, then summarize.
        rows = conn.execute(
            """
            WITH pairs AS (
                SELECT
                    m1.id    AS motion1_id, mt1.meeting_date AS date1, m1.title AS title1,
                    m2.id    AS motion2_id, mt2.meeting_date AS date2, m2.title AS title2,
                    similarity(m1.title, m2.title) AS sim,
                    mt1.governing_body_id AS body_id
                FROM motion m1
                JOIN meeting mt1 ON mt1.id = m1.meeting_id
                JOIN motion m2   ON m2.id > m1.id
                JOIN meeting mt2 ON mt2.id = m2.meeting_id
                WHERE mt1.governing_body_id = mt2.governing_body_id
                  AND similarity(m1.title, m2.title) > %s
                  AND ABS(mt2.meeting_date - mt1.meeting_date) BETWEEN 1 AND 365
            )
            SELECT *, gb.name AS body_name, gb.jurisdiction_id, j.display_name AS jurisdiction_name
            FROM pairs p
            JOIN governing_body gb ON gb.id = p.body_id
            JOIN jurisdiction j ON j.id = gb.jurisdiction_id
            ORDER BY sim DESC, date1 ASC
            """,
            (SIMILARITY_THRESHOLD,),
        ).fetchall()

        # Cluster pairs into groups by transitive closure on motion IDs
        # (so 3 motions appearing in 2 pairs become one cluster of 3)
        from collections import defaultdict
        adj: dict[int, set[int]] = defaultdict(set)
        motion_info: dict[int, dict] = {}
        for r in rows:
            adj[r["motion1_id"]].add(r["motion2_id"])
            adj[r["motion2_id"]].add(r["motion1_id"])
            motion_info[r["motion1_id"]] = {
                "date": r["date1"], "title": r["title1"],
                "body_id": r["body_id"], "body_name": r["body_name"],
                "jurisdiction_id": r["jurisdiction_id"],
                "jurisdiction_name": r["jurisdiction_name"],
            }
            motion_info[r["motion2_id"]] = {
                "date": r["date2"], "title": r["title2"],
                "body_id": r["body_id"], "body_name": r["body_name"],
                "jurisdiction_id": r["jurisdiction_id"],
                "jurisdiction_name": r["jurisdiction_name"],
            }

        visited: set[int] = set()
        clusters: list[list[int]] = []
        for node in adj:
            if node in visited:
                continue
            stack = [node]
            comp: list[int] = []
            while stack:
                cur = stack.pop()
                if cur in visited:
                    continue
                visited.add(cur)
                comp.append(cur)
                stack.extend(adj[cur])
            if len(comp) >= 2:
                clusters.append(sorted(comp, key=lambda m: motion_info[m]["date"]))

        findings: list[Finding] = []
        for cluster in clusters:
            info_list = [motion_info[m] for m in cluster]
            count = len(cluster)
            first_date = info_list[0]["date"]
            last_date = info_list[-1]["date"]
            span_days = (last_date - first_date).days

            severity = 0
            if count >= 3 and span_days <= 365:
                severity = 3
            elif count >= 2 and span_days <= 180:
                severity = 2
            elif count >= 2:
                severity = 1

            # Title cleanup — use the shortest title as representative
            representative = min(info_list, key=lambda i: len(i["title"]))["title"]
            short = representative[:140] + ("..." if len(representative) > 140 else "")

            title = (
                f"Same matter voted on {count} times in {span_days} days "
                f"({first_date} → {last_date}): \"{short}\""
            )
            explanation = (
                "Motions with highly similar titles appearing repeatedly within a year "
                "in the same body usually indicate a proposal being pushed through "
                "iteration: tabled, modified, denied-then-reconsidered, or coming "
                "back with watered-down terms. Repeat attempts on the same matter "
                "are worth examining: who is the petitioner driving the persistence, "
                "and what changed between attempts?"
            )

            findings.append(Finding(
                pattern_id=self.pattern_id,
                severity=severity,
                title=title,
                explanation=explanation,
                jurisdiction_id=info_list[0]["jurisdiction_id"],
                governing_body_id=info_list[0]["body_id"],
                subject_motion_id=cluster[0],
                evidence=[
                    {"motion_id": m, "date": str(motion_info[m]["date"]), "title": motion_info[m]["title"]}
                    for m in cluster
                ],
                metrics={
                    "motion_count": count,
                    "span_days": span_days,
                    "first_date": str(first_date),
                    "last_date": str(last_date),
                },
            ))
        return findings
