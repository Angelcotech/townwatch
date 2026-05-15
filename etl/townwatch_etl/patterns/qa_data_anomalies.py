"""
QA Pattern: qa_data_anomalies

Bundle of deterministic data-quality checks that all share the same
finding pattern_id family. Each finding identifies one specific anomaly
worth operator review or auto-correction.

Sub-checks:
  qa_petitioner_is_staff   — petitioner_name contains a staff title word
  qa_tally_mismatch        — vote tally doesn't equal sum of individual votes
  qa_short_motion_title    — motion title is suspiciously short
  qa_orphan_official       — official with no votes and no current term (likely a misclassified staff name)
  qa_low_confidence_meeting — meeting attendance_notes show extraction_confidence=low
  qa_generic_petitioner    — petitioner_name is a generic value ('Staff', 'Resident')
"""

from __future__ import annotations

import psycopg

from .base import Finding, Pattern


STAFF_TITLE_KEYWORDS = [
    "director", "administrator", "attorney", "clerk", "manager",
    "engineer", "chief", "inspector", "coordinator", "secretary",
    "treasurer", "superintendent", "officer",
]
GENERIC_PETITIONERS = {"staff", "resident", "applicant", "petitioner", "unknown", "n/a"}


class QaPetitionerIsStaff(Pattern):
    pattern_id = "qa_petitioner_is_staff"

    def detect(self, conn: psycopg.Connection) -> list[Finding]:
        rows = conn.execute("""
            SELECT id, petitioner_name, title
            FROM motion WHERE petitioner_name IS NOT NULL
        """).fetchall()

        findings: list[Finding] = []
        for r in rows:
            pet = (r["petitioner_name"] or "").lower()
            matches = [kw for kw in STAFF_TITLE_KEYWORDS if kw in pet]
            if not matches:
                continue
            findings.append(Finding(
                pattern_id=self.pattern_id,
                severity=2,
                title=f"Petitioner field contains staff title: '{r['petitioner_name']}'",
                explanation=(
                    "The model captured a staff member as the petitioner, but staff "
                    "are typically the recommender, not the applicant. Re-review the "
                    "motion or correct the petitioner_name to the actual applicant."
                ),
                subject_motion_id=int(r["id"]),
                metrics={
                    "petitioner_name": r["petitioner_name"],
                    "motion_title": r["title"],
                    "matched_keywords": matches,
                },
            ))
        return findings


class QaTallyMismatch(Pattern):
    pattern_id = "qa_tally_mismatch"

    def detect(self, conn: psycopg.Connection) -> list[Finding]:
        rows = conn.execute("""
            SELECT m.id, m.title,
                   m.vote_tally_yes + m.vote_tally_no + m.vote_tally_abstain + m.vote_tally_absent AS tally_total,
                   COUNT(v.id) AS actual_votes
            FROM motion m
            LEFT JOIN vote v ON v.motion_id = m.id
            WHERE m.vote_tally_yes + m.vote_tally_no + m.vote_tally_abstain + m.vote_tally_absent > 0
            GROUP BY m.id, m.title, m.vote_tally_yes, m.vote_tally_no, m.vote_tally_abstain, m.vote_tally_absent
            HAVING COUNT(v.id) != (m.vote_tally_yes + m.vote_tally_no + m.vote_tally_abstain + m.vote_tally_absent)
        """).fetchall()
        findings: list[Finding] = []
        for r in rows:
            findings.append(Finding(
                pattern_id=self.pattern_id,
                severity=2,
                title=f"Vote tally mismatch: tally={r['tally_total']} actual_votes={r['actual_votes']}",
                explanation=(
                    "The declared vote tally on this motion doesn't match the number of "
                    "individual_votes rows actually inserted. Likely cause: some vote names "
                    "didn't resolve to officials during ingest. Re-run identity resolution "
                    "or examine the raw extraction payload."
                ),
                subject_motion_id=int(r["id"]),
                metrics={
                    "motion_title": r["title"],
                    "declared_tally": int(r["tally_total"]),
                    "actual_votes": int(r["actual_votes"]),
                },
            ))
        return findings


class QaShortMotionTitle(Pattern):
    pattern_id = "qa_short_motion_title"

    def detect(self, conn: psycopg.Connection) -> list[Finding]:
        rows = conn.execute("""
            SELECT id, title FROM motion WHERE LENGTH(title) < 10
        """).fetchall()
        return [
            Finding(
                pattern_id=self.pattern_id,
                severity=1,
                title=f"Suspiciously short motion title: '{r['title']}'",
                explanation="Motion title under 10 characters — likely truncated or extraction noise.",
                subject_motion_id=int(r["id"]),
                metrics={"title": r["title"], "length": len(r["title"])},
            )
            for r in rows
        ]


class QaOrphanOfficial(Pattern):
    pattern_id = "qa_orphan_official"

    def detect(self, conn: psycopg.Connection) -> list[Finding]:
        # Only flag *elected* officials with zero votes and zero terms.
        # Appointed staff legitimately have neither — they're employees,
        # not councilmembers. Deleting them would be the bug, not the cure.
        rows = conn.execute("""
            SELECT o.id, o.canonical_name,
                   (SELECT COUNT(*) FROM vote WHERE official_id = o.id) AS votes,
                   (SELECT COUNT(*) FROM term WHERE official_id = o.id) AS terms
            FROM official o
            WHERE o.is_elected = TRUE
              AND (SELECT COUNT(*) FROM vote WHERE official_id = o.id) = 0
              AND (SELECT COUNT(*) FROM term WHERE official_id = o.id) = 0
        """).fetchall()
        return [
            Finding(
                pattern_id=self.pattern_id,
                severity=1,
                title=f"Orphan official: '{r['canonical_name']}' has no votes and no terms",
                explanation=(
                    "This official record has zero votes attributed and zero terms on file. "
                    "Likely a staff name misclassified as an elected official, or a name "
                    "captured during extraction that didn't connect to any motions. "
                    "Candidate for deletion."
                ),
                subject_official_id=int(r["id"]),
                metrics={"canonical_name": r["canonical_name"]},
            )
            for r in rows
        ]


class QaLowConfidenceMeeting(Pattern):
    pattern_id = "qa_low_confidence_meeting"

    def detect(self, conn: psycopg.Connection) -> list[Finding]:
        rows = conn.execute("""
            SELECT m.id, m.meeting_date, m.attendance_notes
            FROM meeting m
            WHERE m.attendance_notes ILIKE %s
        """, ("%extraction_confidence=low%",)).fetchall()
        return [
            Finding(
                pattern_id=self.pattern_id,
                severity=2,
                title=f"Low-confidence extraction on {r['meeting_date']}",
                explanation=(
                    "The extractor flagged this meeting's extraction as low-confidence "
                    "(significant content illegible or unclear). Review and consider re-extraction."
                ),
                metrics={"meeting_date": str(r["meeting_date"])},
            )
            for r in rows
        ]


class QaOfficialAsPetitioner(Pattern):
    """
    Flag motions where petitioner_name matches an elected official on the
    same body. Almost always an extraction error — councilmembers can move
    or second a motion in their own body, but they're not its petitioner.
    The repair nullifies petitioner_name; the council role is recorded
    separately via motion.movant.

    Match shapes (case-insensitive):
      - petitioner == canonical_name exactly
      - petitioner starts with "Councilmember <last>" or "Councilmember <full>"
      - petitioner starts with "Mayor <full>" or "Mayor Pro Tem <full>"

    Bare last-name match is deliberately not used — too many false positives
    (e.g. "Russell Jones, Jr." matched as a last-name collision with Gary Jones).
    """
    pattern_id = "qa_official_as_petitioner"

    def detect(self, conn: psycopg.Connection) -> list[Finding]:
        # Match against ANY elected official record, not just those with a
        # term row. Historical councilmembers in the corpus have votes
        # attributed (so we know they served) but their term backfill is
        # incomplete — joining through term would miss them.
        rows = conn.execute("""
            SELECT DISTINCT m.id, m.petitioner_name, o.id AS official_id,
                   o.canonical_name, m.title
            FROM motion m
            JOIN official o ON o.is_elected = TRUE
            WHERE m.data_status = 'clean'
              AND m.petitioner_name IS NOT NULL
              AND (
                LOWER(m.petitioner_name) = LOWER(o.canonical_name)
                OR LOWER(m.petitioner_name) LIKE 'councilmember ' || LOWER(o.last_name) || '%'
                OR LOWER(m.petitioner_name) LIKE 'councilmember ' || LOWER(o.canonical_name) || '%'
                OR LOWER(m.petitioner_name) LIKE 'mayor pro tem ' || LOWER(o.canonical_name) || '%'
                OR LOWER(m.petitioner_name) LIKE 'mayor ' || LOWER(o.canonical_name) || '%'
              )
        """).fetchall()
        return [
            Finding(
                pattern_id=self.pattern_id,
                severity=2,
                title=(
                    f"Petitioner field names an elected official: '{r['petitioner_name']}' "
                    f"(matches {r['canonical_name']})"
                ),
                explanation=(
                    "Council members can move, second, or speak to a motion in their "
                    "own body, but they're not the motion's petitioner. The petitioner "
                    "field captured a council member as if they had filed the request "
                    "externally — almost always an extraction confusion between "
                    "'movant' and 'petitioner'. Repair will set petitioner_name to NULL."
                ),
                subject_motion_id=int(r["id"]),
                metrics={
                    "petitioner_name": r["petitioner_name"],
                    "matched_official_id": int(r["official_id"]),
                    "matched_canonical": r["canonical_name"],
                    "motion_title": r["title"],
                },
            )
            for r in rows
        ]


class QaGenericPetitioner(Pattern):
    pattern_id = "qa_generic_petitioner"

    def detect(self, conn: psycopg.Connection) -> list[Finding]:
        rows = conn.execute("""
            SELECT id, title, petitioner_name FROM motion
            WHERE petitioner_name IS NOT NULL
        """).fetchall()
        findings: list[Finding] = []
        for r in rows:
            pet = (r["petitioner_name"] or "").strip().lower()
            if pet in GENERIC_PETITIONERS or len(pet) < 3:
                findings.append(Finding(
                    pattern_id=self.pattern_id,
                    severity=1,
                    title=f"Generic petitioner value: '{r['petitioner_name']}'",
                    explanation=(
                        "Petitioner field is a generic placeholder rather than a specific "
                        "entity. Either the source minutes didn't name the petitioner, "
                        "or the model defaulted to a placeholder."
                    ),
                    subject_motion_id=int(r["id"]),
                    metrics={
                        "petitioner_name": r["petitioner_name"],
                        "motion_title": r["title"],
                    },
                ))
        return findings
