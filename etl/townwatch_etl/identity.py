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
    "Vice-Chairman",
    "Vice Chairman",
    "Vice Chair",
    "Council Member",
    "Councilmember",
    "Councilmeber",   # recurring clerk typo (Grovetown minutes)
    "Councilman",
    "Councilwoman",
    "Alderman",
    "Alderwoman",
    "Commissioner",
    "Supervisor",
    "Chairman",
    "Chair",
    "Mayor",
    # Honorifics — minutes say "Ms. Murray" / "Mr. Dominique Barabino"; the
    # first-name-agreement guard must see the NAME, not the honorific.
    "Mr.", "Mr", "Ms.", "Ms", "Mrs.", "Mrs", "Dr.", "Dr", "Hon.", "Hon",
]

# Generational suffixes are NOT surnames. Before this existed, "Thomas W.
# Mercer, Jr" was created with last_name='Jr' and then 'J. Charles Allen, Jr'
# (a different person, different SURNAME) matched him by "last name" —
# the 2026-06-12 identity-merge corruption.
_SUFFIX_TOKENS = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}

# Tight nickname equivalences for first-name agreement. Deliberately small:
# a missing pair costs one unmatched alias (recoverable); a wrong pair merges
# two people (the unrecoverable failure this module exists to prevent).
_NICKNAMES = {
    "doug": "douglas", "chris": "christopher", "mike": "michael",
    "bob": "robert", "rob": "robert", "bill": "william", "will": "william",
    "jim": "james", "jimmy": "james", "tom": "thomas", "tony": "anthony",
    "rick": "richard", "rich": "richard", "dick": "richard", "ed": "edward",
    "ted": "edward", "dan": "daniel", "dave": "david", "steve": "steven",
    "joe": "joseph", "jacqueline": "jackie", "liz": "elizabeth",
    "beth": "elizabeth", "kate": "katherine", "kathy": "katherine",
    "sue": "susan", "peggy": "margaret", "jack": "john",
}


def split_person_name(name: str) -> dict:
    """Split a person name into first/middle/last/suffix, suffix-aware:
    'Thomas W. Mercer, Jr' → first='Thomas', middle='W.', last='Mercer',
    suffix='Jr'. Commas are separators, never part of a token."""
    cleaned = name.replace(",", " ").strip()
    tokens = [t for t in cleaned.split() if t]
    suffix = None
    if len(tokens) > 1 and tokens[-1].lower() in _SUFFIX_TOKENS:
        suffix = tokens[-1]
        tokens = tokens[:-1]
    if not tokens:
        return {"first": None, "middle": None, "last": None, "suffix": suffix}
    if len(tokens) == 1:
        return {"first": None, "middle": None, "last": tokens[0], "suffix": suffix}
    return {
        "first": tokens[0],
        "middle": " ".join(tokens[1:-1]) or None,
        "last": tokens[-1],
        "suffix": suffix,
    }


def first_names_agree(a: str | None, b: str | None) -> bool:
    """Whether two FIRST names can belong to the same person: exact match,
    initial-vs-full ('J.' agrees with 'James'), or a known nickname pair.
    Absent names agree with anything (a surname-only reference carries no
    first-name evidence) — the CALLER decides whether surname-only evidence
    is sufficient; this function only refuses contradictions."""
    if not a or not b:
        return True
    a, b = a.lower().rstrip("."), b.lower().rstrip(".")
    if a == b:
        return True
    if len(a) == 1 or len(b) == 1:          # initial vs full
        return a[0] == b[0]
    return _NICKNAMES.get(a, a) == _NICKNAMES.get(b, b)


def looks_unresolvable(name: str) -> bool:
    """Names that must never create or match an official: extraction
    annotations ('(... illegible)'), digits, or no alphabetic content.
    Illegible deed signatories became a phantom official before this guard."""
    if not name or not any(c.isalpha() for c in name):
        return True
    low = name.lower()
    return ("illegible" in low or "unknown" in low or "(" in name
            or any(c.isdigit() for c in name))


def strip_title(name: str) -> str:
    """Remove leading elected-office titles and honorifics from a name string.
    Iterative, so 'Councilwoman Ms. Sylvia Martin' → 'Sylvia Martin'.
    """
    s = name.strip()
    changed = True
    while changed:
        changed = False
        s_lower = s.lower()
        for title in _TITLE_PATTERNS:
            prefix = title.lower() + " "
            if s_lower.startswith(prefix):
                s = s[len(title):].strip()
                changed = True
                break
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


# =====================================================================
# CachedResolver — batch-job identity resolution
# =====================================================================

class CachedResolver:
    """
    In-memory identity resolver for batch jobs.

    Loads all aliases + last-name → official_id mappings at construction.
    Resolution is O(1) thereafter. Newly discovered aliases get recorded
    to both the DB and the cache so subsequent lookups stay consistent.

    Typical usage inside an IngestJob:
        self.resolver = CachedResolver(
            self.conn,
            jurisdiction_id=jid,
            data_source_id=self.data_source_id,
            source_system=self.source_name,
        )
        for name in names:
            oid = self.resolver.resolve(name)

    Designed to be the default identity-resolution surface for any job
    that resolves more than a handful of names — eliminates the per-name
    network round-trip that dominates Railway-Postgres batch jobs.
    """

    def __init__(
        self,
        conn: psycopg.Connection,
        *,
        jurisdiction_id: int | None = None,
        data_source_id: int | None = None,
        source_system: str = "cached_resolver",
    ) -> None:
        self.conn = conn
        self.jurisdiction_id = jurisdiction_id
        self.data_source_id = data_source_id
        self.source_system = source_system
        self._alias_map: dict[str, int] = {}
        self._last_name_map: dict[str, list[int]] = {}
        self.refresh()

    def refresh(self) -> None:
        """Re-load caches from DB. Call after bulk official creation."""
        self._alias_map.clear()
        self._last_name_map.clear()
        self._first_name_map: dict[int, str | None] = {}
        self._term_holders: set[int] = set()

        for r in self.conn.execute(
            "SELECT alias_name, official_id FROM official_alias"
        ).fetchall():
            self._alias_map[r["alias_name"].lower()] = r["official_id"]

        for r in self.conn.execute(
            "SELECT id, first_name FROM official"
        ).fetchall():
            self._first_name_map[r["id"]] = r["first_name"]

        # Officials who have (ever) held a seat — the only people a bare
        # surname reference in MINUTES can mean. Staff with a matching
        # surname must not absorb a council member's votes (the Ceretta
        # Smith → Bradley Smith corruption, 2026-06-12).
        holder_sql = "SELECT DISTINCT official_id FROM term"
        params: tuple = ()
        if self.jurisdiction_id is not None:
            holder_sql = """
                SELECT DISTINCT t.official_id FROM term t
                JOIN seat s ON s.id = t.seat_id
                JOIN governing_body gb ON gb.id = s.governing_body_id
                WHERE gb.jurisdiction_id = %s"""
            params = (self.jurisdiction_id,)
        for r in self.conn.execute(holder_sql, params).fetchall():
            self._term_holders.add(r["official_id"])

        if self.jurisdiction_id is None:
            rows = self.conn.execute(
                "SELECT id, LOWER(last_name) AS ln FROM official"
            ).fetchall()
        else:
            rows = self.conn.execute("""
                SELECT DISTINCT o.id, LOWER(o.last_name) AS ln
                FROM official o
                LEFT JOIN term t       ON t.official_id = o.id
                LEFT JOIN seat s       ON s.id = t.seat_id
                LEFT JOIN governing_body gb ON gb.id = s.governing_body_id
                WHERE gb.jurisdiction_id = %s OR gb.id IS NULL
            """, (self.jurisdiction_id,)).fetchall()

        for r in rows:
            ln = r["ln"]
            if ln and r["id"] not in self._last_name_map.get(ln, []):
                self._last_name_map.setdefault(ln, []).append(r["id"])

        # Also include all officials regardless of term (historical)
        for r in self.conn.execute(
            "SELECT id, LOWER(last_name) AS ln FROM official"
        ).fetchall():
            ln = r["ln"]
            if ln and r["id"] not in self._last_name_map.get(ln, []):
                self._last_name_map.setdefault(ln, []).append(r["id"])

    def resolve(self, source_name: str) -> int | None:
        """Return official_id or None. Pure in-memory after initial refresh.

        Surname matching requires FIRST-NAME AGREEMENT when the reference
        carries one, and a bare surname only resolves to a term-holder —
        last-name-only matching against the whole officials table merged
        different people into one row (Ceretta Smith → Bradley Smith,
        Michael→Beverly Tuttle, three Brazells; fixed 2026-06-12)."""
        if not source_name or looks_unresolvable(source_name):
            return None

        oid = self._alias_map.get(source_name.lower())
        if oid is not None:
            return oid

        stripped = strip_title(source_name)
        oid = self._alias_map.get(stripped.lower())
        if oid is not None:
            self._record_alias(oid, source_name)
            return oid

        parts = split_person_name(stripped)
        if not parts["last"]:
            return None
        candidates = self._last_name_map.get(parts["last"].lower(), [])

        if parts["first"]:
            agreeing = [c for c in candidates
                        if first_names_agree(parts["first"], self._first_name_map.get(c))]
            if len(agreeing) == 1:
                oid = agreeing[0]
                self._record_alias(oid, source_name)
                return oid
            return None

        # Bare surname ("Councilmember Smith"): only a seat-holder can be
        # meant, and only an unambiguous one.
        holders = [c for c in candidates if c in self._term_holders]
        if len(holders) == 1:
            oid = holders[0]
            self._record_alias(oid, source_name)
            return oid
        return None

    def record_alias(self, official_id: int, alias_name: str) -> None:
        """Explicit external alias addition. Idempotent."""
        self._record_alias(official_id, alias_name)

    def register_new_official(
        self, official_id: int, *, canonical_name: str, last_name: str
    ) -> None:
        """Caller created a new official mid-run; update caches without re-querying."""
        self._alias_map[canonical_name.lower()] = official_id
        ln = (last_name or "").lower()
        if ln and official_id not in self._last_name_map.get(ln, []):
            self._last_name_map.setdefault(ln, []).append(official_id)

    def _record_alias(self, official_id: int, alias_name: str) -> None:
        key = alias_name.lower()
        if key in self._alias_map:
            return
        # Regression guard: never attach an alias whose first name
        # contradicts the official's. A wrong alias is permanent — every
        # future resolution of that name inherits the error.
        parts = split_person_name(strip_title(alias_name))
        if not first_names_agree(parts["first"], self._first_name_map.get(official_id)):
            print(f"  ⚠ identity: refusing alias {alias_name!r} for official "
                  f"#{official_id} (first-name conflict with "
                  f"{self._first_name_map.get(official_id)!r})")
            return
        if self.data_source_id is None:
            return  # caller hasn't wired provenance yet; cache-only
        add_alias(
            self.conn,
            official_id=official_id,
            alias_name=alias_name,
            source_system=self.source_system,
            data_source_id=self.data_source_id,
        )
        self._alias_map[key] = official_id
