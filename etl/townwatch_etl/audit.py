"""
Audit-pipeline helpers — shared between jobs.

Two responsibilities:
  1. Load per-state open-records / open-meetings law config from
     jurisdictions/_open_records_laws.json so observers and PDF generators
     stay state-agnostic.
  2. Record structured failures to pipeline_failure so problems surface
     loudly instead of disappearing into stderr.

No silent fallbacks. If a state's law config is missing, that's an error
the operator should see — it's not a default.
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path
from typing import Any


_LAWS_PATH = Path(__file__).resolve().parents[2] / "jurisdictions" / "_open_records_laws.json"
_LAWS_CACHE: dict[str, Any] | None = None


def load_state_laws() -> dict[str, Any]:
    """Read the full open-records-laws config. Cached after first read."""
    global _LAWS_CACHE
    if _LAWS_CACHE is None:
        with _LAWS_PATH.open() as f:
            _LAWS_CACHE = json.load(f)
    return _LAWS_CACHE


def state_law(state_abbr: str) -> dict[str, Any]:
    """Get the law config for a state. Raises if the state isn't configured —
    the caller should handle this by recording a pipeline_failure and skipping
    the body, not by falling back silently to GA."""
    laws = load_state_laws()
    key = (state_abbr or "").upper()
    if key not in laws:
        raise KeyError(
            f"No open-records-law config for state {state_abbr!r}. "
            f"Add an entry to jurisdictions/_open_records_laws.json keyed by "
            f"the two-letter state code (uppercase)."
        )
    return laws[key]


def finding_statute(state_abbr: str, category: str) -> dict[str, str]:
    """Return the statute citation block for a (state, finding category).
    Shape: {statute_label, statute_url, statute_text}. Raises on misconfig."""
    s = state_law(state_abbr)
    cats = s.get("finding_categories", {})
    if category not in cats:
        raise KeyError(
            f"State {state_abbr!r} has no finding_categories.{category} configured. "
            f"Add a citation block to _open_records_laws.json."
        )
    block = cats[category]
    required = {"statute_label", "statute_url", "statute_text"}
    missing = required - set(block.keys())
    if missing:
        raise KeyError(
            f"State {state_abbr!r} finding_categories.{category} is missing: {missing}"
        )
    return block


def record_failure(
    conn,
    *,
    job_name: str,
    message: str,
    step: str | None = None,
    governing_body_id: int | None = None,
    meeting_id: int | None = None,
    finding_id: int | None = None,
    exception: BaseException | None = None,
    context: dict | None = None,
) -> int:
    """Persist a structured failure record. Returns the new row id.

    Call this in every job's exception handler. Never swallow an exception
    without recording it — silent failures are the worst kind in an audit
    pipeline because they hide the gap in the gap-detector.
    """
    exc_class = type(exception).__name__ if exception else None
    tb = "".join(traceback.format_exception(type(exception), exception, exception.__traceback__)) if exception else None
    row = conn.execute(
        """
        INSERT INTO pipeline_failure (
            job_name, step, governing_body_id, meeting_id, finding_id,
            exception_class, message, context, traceback
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
        RETURNING id
        """,
        (
            job_name, step, governing_body_id, meeting_id, finding_id,
            exc_class, message,
            json.dumps(context) if context is not None else None,
            tb,
        ),
    ).fetchone()
    # Echo to stderr so the operator sees it even before checking the table.
    import sys
    print(
        f"  ✗ FAILURE recorded [{job_name}{':' + step if step else ''}] {message}",
        file=sys.stderr,
    )
    return row["id"]
