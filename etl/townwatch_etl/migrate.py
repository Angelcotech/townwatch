"""
Apply pending migrations from the migrations/ directory.

Migrations are SQL files named NNN_description.sql at the repo root's
migrations/ directory. The schema_migrations table tracks what's been
applied so each migration only runs once.

Run:
    python -m townwatch_etl.migrate
"""

from __future__ import annotations

import sys
from pathlib import Path

from .db import connect


MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT        PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def main() -> int:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print(f"No migration files found in {MIGRATIONS_DIR}")
        return 1

    print(f"Migrations dir: {MIGRATIONS_DIR}")
    print(f"Found {len(files)} file(s)\n")

    with connect() as conn:
        conn.execute(SCHEMA_MIGRATIONS_DDL)
        applied = {
            r["version"]
            for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }

        for f in files:
            version = f.stem  # "001_initial_schema"
            if version in applied:
                print(f"  ⊘ {f.name} (already applied)")
                continue
            print(f"  → applying {f.name} ...")
            sql = f.read_text()
            conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s)",
                (version,),
            )
            print(f"  ✓ {f.name}")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
