"""
Identity resolution — map name strings from any source to the canonical official.

The fundamental problem: "Bob Smith" (assessor), "Robert J. Smith" (ballot),
and "R. Smith" (campaign filing) are the same person, but no source uses a
single canonical name. Get this wrong and votes don't connect to donations,
donations don't connect to property records, and the entire thesis of the
platform breaks.

Strategy (deterministic, never auto-creates):
    1. Exact match on official_alias.alias_name → return the official_id
    2. Fuzzy match via pg_trgm similarity, ranked
    3. Caller decides what to do with the candidates list

Auto-creation is intentionally not provided here. The cost of a duplicate
official is permanent data fragmentation; the cost of explicit creation is
one extra line in the ingest job. We pay the explicit price.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg


SIMILARITY_THRESHOLD = 0.65  # pg_trgm similarity; tuned during real ingest


# Common elected-office titles to strip when a source includes them as prefixes
# ("Mayor Pro-Tem Eric Blair" → "Eric Blair"). Order matters: longer phrases first.
_TITLE_PATTERNS = [
    "Mayor Pro-Tem",
    "Mayor Pro Tem",
    "Vice Mayor",
    "Council Member",
    "Councilmember",
    "Councilman",
    "Councilwoman",
    "Alderman",
    "Alderwoman",
    "Commissioner",
    "Supervisor",
    "Chair",
    "Vice Chair",
    "Mayor",
]


def strip_title(name: str) -> str:
    """Remove a leading elected-office title from a name string.
    'Councilmember A. Richard Bowman' → 'A. Richard Bowman'
    """
    s = name.strip()
    s_lower = s.lower()
    for title in _TITLE_PATTERNS:
        prefix = title.lower() + " "
        if s_lower.startswith(prefix):
            return s[len(title):].strip()
    return s


@dataclass
class OfficialCandidate:
    official_id: int
    canonical_name: str
    similarity: float
    matched_via: str  # 'exact_alias' | 'canonical_name_trgm' | 'alias_name_trgm'


def find_by_alias(
    conn: psycopg.Connection,
    alias_name: str,
) -> int | None:
    """Exact match on official_alias.alias_name. Case-insensitive."""
    row = conn.execute(
        "SELECT official_id FROM official_alias WHERE LOWER(alias_name) = LOWER(%s) LIMIT 1",
        (alias_name,),
    ).fetchone()
    return row["official_id"] if row else None


def find_by_last_name_active_at(
    conn: psycopg.Connection,
    name_chunk: str,
    *,
    jurisdiction_id: int,
    as_of_date,
) -> list[tuple[int, str]]:
    """
    Find officials in this jurisdiction whose last_name matches the last word
    of name_chunk AND who held a term covering as_of_date.

    If no date-bounded matches exist, fall back to officials with that last_name
    in this jurisdiction at any time. Phase 1 jurisdiction configs often store
    a placeholder term start_date because actual election dates aren't yet known.

    Returns (official_id, canonical_name) tuples — caller decides how to handle
    multiple matches (typically: disambiguate by first initial).
    """
    last_name_candidate = name_chunk.strip().split()[-1]

    rows = conn.execute(
        """
        SELECT DISTINCT o.id, o.canonical_name
        FROM official o
        JOIN term t            ON t.official_id = o.id
        JOIN seat s            ON s.id = t.seat_id
        JOIN governing_body gb ON gb.id = s.governing_body_id
        WHERE gb.jurisdiction_id = %s
          AND LOWER(o.last_name) = LOWER(%s)
          AND t.start_date <= %s
          AND (t.end_date IS NULL OR t.end_date >= %s)
        """,
        (jurisdiction_id, last_name_candidate, as_of_date, as_of_date),
    ).fetchall()
    if rows:
        return [(r["id"], r["canonical_name"]) for r in rows]

    # Fallback: any term in this jurisdiction with this last_name
    rows = conn.execute(
        """
        SELECT DISTINCT o.id, o.canonical_name
        FROM official o
        JOIN term t            ON t.official_id = o.id
        JOIN seat s            ON s.id = t.seat_id
        JOIN governing_body gb ON gb.id = s.governing_body_id
        WHERE gb.jurisdiction_id = %s
          AND LOWER(o.last_name) = LOWER(%s)
        """,
        (jurisdiction_id, last_name_candidate),
    ).fetchall()
    return [(r["id"], r["canonical_name"]) for r in rows]


def find_candidates(
    conn: psycopg.Connection,
    name: str,
    *,
    jurisdiction_id: int | None = None,
    limit: int = 5,
) -> list[OfficialCandidate]:
    """
    Find canonical officials by fuzzy match against canonical_name and any alias.
    Optionally filter by jurisdiction (via current term → seat → governing_body → jurisdiction).
    Returns candidates sorted by similarity descending.
    """
    candidates: dict[int, OfficialCandidate] = {}

    # 1. Canonical name match
    sql_canon = """
        SELECT o.id, o.canonical_name, similarity(o.canonical_name, %s) AS sim
        FROM official o
        WHERE similarity(o.canonical_name, %s) >= %s
        ORDER BY sim DESC
        LIMIT %s
    """
    for row in conn.execute(sql_canon, (name, name, SIMILARITY_THRESHOLD, limit)).fetchall():
        candidates[row["id"]] = OfficialCandidate(
            official_id=row["id"],
            canonical_name=row["canonical_name"],
            similarity=float(row["sim"]),
            matched_via="canonical_name_trgm",
        )

    # 2. Alias match (only add if a better score doesn't already exist)
    sql_alias = """
        SELECT o.id, o.canonical_name, MAX(similarity(a.alias_name, %s)) AS sim
        FROM official o
        JOIN official_alias a ON a.official_id = o.id
        WHERE similarity(a.alias_name, %s) >= %s
        GROUP BY o.id, o.canonical_name
        ORDER BY sim DESC
        LIMIT %s
    """
    for row in conn.execute(sql_alias, (name, name, SIMILARITY_THRESHOLD, limit)).fetchall():
        existing = candidates.get(row["id"])
        sim = float(row["sim"])
        if existing is None or sim > existing.similarity:
            candidates[row["id"]] = OfficialCandidate(
                official_id=row["id"],
                canonical_name=row["canonical_name"],
                similarity=sim,
                matched_via="alias_name_trgm",
            )

    out = sorted(candidates.values(), key=lambda c: c.similarity, reverse=True)

    if jurisdiction_id is not None:
        # Narrow to officials with at least one term tied to this jurisdiction
        ids = [c.official_id for c in out]
        if not ids:
            return []
        rows = conn.execute(
            """
            SELECT DISTINCT t.official_id
            FROM term t
            JOIN seat s         ON s.id = t.seat_id
            JOIN governing_body gb ON gb.id = s.governing_body_id
            WHERE gb.jurisdiction_id = %s AND t.official_id = ANY(%s)
            """,
            (jurisdiction_id, ids),
        ).fetchall()
        allowed = {r["official_id"] for r in rows}
        out = [c for c in out if c.official_id in allowed]

    return out


def resolve(
    conn: psycopg.Connection,
    name: str,
    *,
    source_system: str,
    jurisdiction_id: int | None = None,
) -> int | None:
    """
    Single-shot resolver: returns an official_id only when we have high confidence.
    - Exact alias match → return immediately.
    - Fuzzy with similarity >= 0.90 AND only one candidate → return.
    - Otherwise → return None and let the caller log/handle the ambiguity.
    """
    exact = find_by_alias(conn, name)
    if exact is not None:
        return exact

    candidates = find_candidates(conn, name, jurisdiction_id=jurisdiction_id)
    if len(candidates) == 1 and candidates[0].similarity >= 0.90:
        return candidates[0].official_id

    return None


def create_official(
    conn: psycopg.Connection,
    *,
    data_source_id: int,
    canonical_name: str,
    last_name: str,
    first_name: str | None = None,
    middle_name: str | None = None,
    suffix: str | None = None,
    party_affiliation: str | None = None,
    photo_url: str | None = None,
    bio_text: str | None = None,
    official_website: str | None = None,
    email: str | None = None,
    phone: str | None = None,
) -> int:
    """Explicit creation of a canonical official. Callers must decide when to do this."""
    row = conn.execute(
        """
        INSERT INTO official
            (canonical_name, first_name, middle_name, last_name, suffix,
             party_affiliation, photo_url, bio_text, official_website,
             email, phone, data_source_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (canonical_name, first_name, middle_name, last_name, suffix,
         party_affiliation, photo_url, bio_text, official_website,
         email, phone, data_source_id),
    ).fetchone()
    assert row is not None
    return row["id"]


def add_alias(
    conn: psycopg.Connection,
    *,
    official_id: int,
    alias_name: str,
    source_system: str,
    data_source_id: int,
) -> None:
    """Record a name variant. Idempotent — does nothing on conflict."""
    conn.execute(
        """
        INSERT INTO official_alias (official_id, alias_name, source_system, data_source_id)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (official_id, alias_name) DO NOTHING
        """,
        (official_id, alias_name, source_system, data_source_id),
    )
