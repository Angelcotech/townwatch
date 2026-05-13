"""
Quick DB connectivity + extensions diagnostic.

Run:
    python -m townwatch_etl.check_db

Reports:
    - Postgres version
    - Whether postgis, pg_trgm, uuid-ossp are installed AND available
    - Total table count (sanity check for fresh DB vs existing)
"""

from __future__ import annotations

import sys

from .db import connect


REQUIRED_EXTENSIONS = ["pg_trgm", "uuid-ossp"]
DEFERRED_EXTENSIONS = ["postgis"]  # added in a future migration


def _ext_status(conn, ext: str) -> tuple[str, str]:
    row = conn.execute(
        """
        SELECT
            EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = %s) AS available,
            EXISTS (SELECT 1 FROM pg_extension          WHERE extname = %s) AS installed
        """,
        (ext, ext),
    ).fetchone()
    assert row is not None
    return ("✓ avail" if row["available"] else "✗ avail",
            "✓ inst" if row["installed"] else "○ inst")


def main() -> int:
    try:
        with connect() as conn:
            # Postgres version
            version = conn.execute("SELECT version()").fetchone()
            assert version is not None
            print(f"✓ Connected.")
            print(f"  {version['version']}")
            print()

            # Available + installed extensions
            print("Required extensions:")
            for ext in REQUIRED_EXTENSIONS:
                avail, installed = _ext_status(conn, ext)
                print(f"  {avail}  {installed}   {ext}")
            print()
            print("Deferred extensions (added later):")
            for ext in DEFERRED_EXTENSIONS:
                avail, installed = _ext_status(conn, ext)
                print(f"  {avail}  {installed}   {ext}")
            print()

            # Existing table count
            tables = conn.execute("""
                SELECT count(*) AS n FROM information_schema.tables
                WHERE table_schema = 'public'
            """).fetchone()
            assert tables is not None
            print(f"Existing tables in public schema: {tables['n']}")

    except Exception as e:
        print(f"✗ Connection failed: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
