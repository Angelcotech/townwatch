"""
Tiny shared helper for per-item failure isolation in batch jobs.

Unattended jobs that process many items in one process (e.g. forum_tick runs
extract_packets / submit_comments with --all across every town) must not let one
bad item abort the whole run. The pattern is: wrap each item in try/except,
record the failure here, and continue. Recording is best-effort and never
raises — isolation is the priority.
"""

from __future__ import annotations

from ..db import connect


def record_process_error(job_name: str, meeting_id, exc: Exception) -> None:
    """Record a per-item batch failure to pipeline_failure. Best-effort: a
    failure to record must never break the loop that's trying to keep going."""
    try:
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO pipeline_failure (job_name, step, meeting_id, exception_class, message)
                VALUES (%s, 'PROCESS_ERROR', %s, %s, %s)
                """,
                (job_name, meeting_id, type(exc).__name__, str(exc)[:1000]),
            )
    except Exception:
        pass
