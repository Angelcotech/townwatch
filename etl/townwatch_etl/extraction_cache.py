"""
Content-addressed extraction cache — never pay twice for the same document.

The extraction of a document is a pure function of (document bytes, extractor
version): same input → same output. So we key the result by sha256(document
bytes) + doc kind + extractor version. A re-run / resume / outage-restart hashes
the freshly-downloaded document, finds the cached result, and replays it into
the DB with ZERO model spend. The model is called only on a genuine miss (new
document, or a deliberately bumped extractor version).

This is the single module that touches extraction_cache (migration 029).
Generic over doc kind so minutes / agendas / any future extractor share it.

Invalidation: bump the caller's *_EXTRACTOR_VERSION constant when the prompt,
schema, or model changes — old rows stop matching and the next process
re-extracts under the new version (old rows are kept for audit).
"""

from __future__ import annotations

import hashlib

from psycopg.types.json import Json


def content_hash(data: bytes) -> str:
    """sha256 hex of the source document bytes — the cache key."""
    return hashlib.sha256(data).hexdigest()


def get(conn, hash_: str, doc_kind: str, extractor_version: str) -> dict | None:
    """Return the cached row ({extraction, method, source_url, cost_usd, ...}) or
    None on a miss."""
    return conn.execute(
        "SELECT extraction, method, source_url, cost_usd, created_at "
        "FROM extraction_cache "
        "WHERE content_hash = %s AND doc_kind = %s AND extractor_version = %s",
        (hash_, doc_kind, extractor_version),
    ).fetchone()


def put(conn, hash_: str, doc_kind: str, extractor_version: str, *,
        extraction_json: str, method: str | None = None,
        source_url: str | None = None, cost_usd=None) -> None:
    """Store a freshly-produced extraction. Idempotent: a second producer for the
    same (document, kind, version) is ignored (first write wins)."""
    conn.execute(
        "INSERT INTO extraction_cache "
        "(content_hash, doc_kind, extractor_version, source_url, method, extraction, cost_usd) "
        "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s) "
        "ON CONFLICT (content_hash, doc_kind, extractor_version) DO NOTHING",
        (hash_, doc_kind, extractor_version, source_url, method, extraction_json, cost_usd),
    )
