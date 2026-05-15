"""
Repair handler: an "official" with zero votes and zero terms is almost
always a staff member that got mis-captured as an elected official. We
delete the spurious record (and its aliases + findings).

Guardrails before deletion:
  1. Pattern must be qa_orphan_official
  2. Zero votes and zero terms (re-checked at repair time)
  3. Name appears in at least one meeting's staff_present array, OR the
     name has a staff-title prefix (Major, Capt., Director, etc.)

If guardrails fail, the finding is left as UNREPAIRABLE — manual review.
"""

from __future__ import annotations

import json

import psycopg

from .base import RepairHandler, RepairOutcome, RepairResult


STAFF_TITLE_PREFIXES = (
    "major ", "capt. ", "capt ", "captain ", "lt. ", "lieutenant ",
    "sgt. ", "sergeant ", "chief ", "officer ",
    "director ", "administrator ", "attorney ", "clerk ", "engineer ",
    "manager ", "coordinator ", "secretary ", "treasurer ", "superintendent ",
)


class OrphanOfficialHandler(RepairHandler):
    handler_id = "orphan_official_delete"

    def can_handle(self, finding: dict, official: dict) -> bool:
        return finding.get("pattern_id") == "qa_orphan_official"

    def repair(self, conn: psycopg.Connection, finding: dict, official: dict) -> RepairResult:
        oid = official["id"]
        name = official["canonical_name"]

        # Re-verify zero votes + zero terms at repair time (defensive — the
        # database could have changed since the QA run that produced this finding).
        votes = conn.execute(
            "SELECT COUNT(*) AS n FROM vote WHERE official_id = %s", (oid,)
        ).fetchone()["n"]
        terms = conn.execute(
            "SELECT COUNT(*) AS n FROM term WHERE official_id = %s", (oid,)
        ).fetchone()["n"]
        if votes > 0 or terms > 0:
            return RepairResult(
                outcome=RepairOutcome.UNREPAIRABLE,
                handler=self.handler_id,
                notes=f"Official has {votes} vote(s) and {terms} term(s); no longer orphan.",
            )

        # Confirm staff signal: appears in staff_present somewhere, OR has staff prefix
        staff_meetings = conn.execute("""
            SELECT COUNT(DISTINCT m.id) AS n
            FROM meeting m, jsonb_array_elements_text(m.staff_present) AS entry
            WHERE m.staff_present IS NOT NULL
              AND LOWER(entry) LIKE %s
        """, (f"%{(official.get('last_name') or '').lower()}%",)).fetchone()["n"]

        lower_name = name.lower()
        has_staff_prefix = any(lower_name.startswith(p) for p in STAFF_TITLE_PREFIXES)

        if staff_meetings == 0 and not has_staff_prefix:
            return RepairResult(
                outcome=RepairOutcome.UNREPAIRABLE,
                handler=self.handler_id,
                notes=(
                    f"Cannot confirm '{name}' is staff — no staff_present matches "
                    "and no staff-title prefix. Leaving for manual review."
                ),
            )

        # Collect audit data BEFORE deletion (so the log row is useful)
        aliases = conn.execute(
            "SELECT alias_name, source_system FROM official_alias WHERE official_id = %s",
            (oid,),
        ).fetchall()
        alias_records = [dict(a) for a in aliases]

        # Delete dependents → finding rows → official row
        conn.execute("DELETE FROM official_alias WHERE official_id = %s", (oid,))
        finding_count = conn.execute(
            "DELETE FROM finding WHERE subject_official_id = %s RETURNING id", (oid,)
        ).fetchall()
        conn.execute("DELETE FROM official WHERE id = %s", (oid,))

        return RepairResult(
            outcome=RepairOutcome.REPAIRED,
            handler=self.handler_id,
            notes=(
                f"Deleted spurious official '{name}' (id={oid}) — appears as staff in "
                f"{staff_meetings} meeting(s). Removed {len(alias_records)} alias(es) "
                f"and {len(finding_count)} finding(s)."
            ),
            mutations={
                "deleted_official_id": oid,
                "canonical_name": name,
                "aliases": alias_records,
                "staff_meeting_count": staff_meetings,
                "deletion_audit": json.dumps({
                    "official_id": oid,
                    "canonical_name": name,
                    "first_name": official.get("first_name"),
                    "last_name": official.get("last_name"),
                    "aliases": alias_records,
                }),
            },
        )
