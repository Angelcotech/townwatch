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


def _resolve_body_types(state_abbr: str, applies_to: list[str]) -> set[str]:
    """Expand class tokens (all_agencies, all_elected, all_appointed,
    all_levying_authorities, …) into concrete body_types via the per-state
    body_type_classes map. Unknown tokens are treated as literal body_types."""
    classes = state_law(state_abbr).get("body_type_classes", {})
    out: set[str] = set()
    for tok in applies_to:
        out |= set(classes.get(tok, [tok]))
    return out


def finding_applies(state_abbr: str, category: str, body_type: str | None) -> bool:
    """Whether a finding category applies to a body of this type, per the
    per-state catalog's applies_to_body_types (resolved through body_type_classes).
    A category with no applies_to_body_types applies to every body (back-compat)."""
    block = state_law(state_abbr).get("finding_categories", {}).get(category)
    if not block:
        return False
    applies = block.get("applies_to_body_types")
    if not applies:
        return True
    return body_type in _resolve_body_types(state_abbr, applies)


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


def mark_url_unreachable(
    conn,
    *,
    meeting_id: int,
    kind: str,             # 'agenda' or 'minutes'
    reason: str,           # '404', 'oversized', 'connection_refused', etc.
    detail: str | None = None,
) -> None:
    """Record on the meeting that one of its scraped URLs is permanently
    unreachable, so future extract-pending queries skip it.

    Lives on meeting.meta as a nested object — no schema migration needed,
    queryable via @> operator. Idempotent: setting the same key again just
    refreshes the checked_at timestamp.

    Used by every job that downloads a document URL (extract_agendas[_batch],
    extract_minutes[_batch], refresh_council_roster). The pending-meetings
    query in each batch driver excludes meetings whose target URL has any
    status entry, so we don't pay the download cost again for dead URLs.
    """
    if kind not in ("agenda", "minutes"):
        raise ValueError(f"kind must be 'agenda' or 'minutes', got {kind!r}")
    field = f"{kind}_url_status"
    import json as _json
    conn.execute(
        """
        UPDATE meeting
        SET meta = COALESCE(meta, '{}'::jsonb) || jsonb_build_object(
                %s,
                jsonb_build_object(
                    'status', 'unreachable',
                    'reason', %s,
                    'detail', %s,
                    'checked_at', now()
                )
            ),
            updated_at = now()
        WHERE id = %s
        """,
        (field, reason, detail, meeting_id),
    )


def mark_url_healthy(conn, *, meeting_id: int, kind: str) -> None:
    """Clear the unreachable status for a URL — used when a fresh
    inventory scrape brings back a URL that previously failed, so
    operators can re-trigger extraction without manual DB edits."""
    if kind not in ("agenda", "minutes"):
        raise ValueError(f"kind must be 'agenda' or 'minutes', got {kind!r}")
    field = f"{kind}_url_status"
    conn.execute(
        "UPDATE meeting SET meta = meta - %s, updated_at = now() WHERE id = %s",
        (field, meeting_id),
    )


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
