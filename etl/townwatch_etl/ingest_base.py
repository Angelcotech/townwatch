"""
IngestJob base — every ETL job inherits from this.

Lifecycle:
    1. open_run()       creates a data_source row and an ingest_run_id UUID
    2. ingest()         subclass-defined work; uses self.insert(...) for writes
    3. close_run()      records summary stats; sets notes on the data_source row

Every write goes through self.insert(), which automatically attaches
self.data_source_id. The run_id groups all records pulled in one invocation
so a bad ingest can be queried back out: WHERE data_source.ingest_run_id = X.
"""

from __future__ import annotations

import json
import uuid
from abc import ABC, abstractmethod
from typing import Any

import psycopg

from .config import DRY_RUN
from .db import connect


class IngestJob(ABC):
    source_name: str           # subclass: 'cityofgrovetown.com', 'FollowTheMoney', etc.
    source_type: str           # subclass: 'api' | 'bulk_download' | 'scrape' | 'manual'
    source_url: str | None = None

    def __init__(self) -> None:
        self.conn: psycopg.Connection | None = None
        self.data_source_id: int | None = None
        self.run_id: str = str(uuid.uuid4())
        self.rows_written: int = 0
        self.rows_skipped: int = 0

    # -- lifecycle -----------------------------------------------------------

    def open_run(self, record_url: str | None = None, raw_payload: Any | None = None) -> None:
        """Create the data_source row. Must be called before any insert()."""
        assert self.conn is not None, "open_run requires an active connection"
        payload_json = json.dumps(raw_payload) if raw_payload is not None else None
        row = self.conn.execute(
            """
            INSERT INTO data_source
                (source_name, source_type, source_url, record_url, ingest_run_id, raw_payload)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (self.source_name, self.source_type, self.source_url, record_url, self.run_id, payload_json),
        ).fetchone()
        assert row is not None
        self.data_source_id = row["id"]

    def close_run(self, notes: str | None = None) -> None:
        """Annotate the data_source row with run summary."""
        assert self.conn is not None and self.data_source_id is not None
        summary = (
            f"rows_written={self.rows_written} rows_skipped={self.rows_skipped}"
            + (f" — {notes}" if notes else "")
        )
        self.conn.execute(
            "UPDATE data_source SET notes = %s WHERE id = %s",
            (summary, self.data_source_id),
        )

    # -- write helpers -------------------------------------------------------

    def insert(self, table: str, data: dict[str, Any]) -> int | None:
        """
        Insert one row. Automatically attaches data_source_id.
        Returns the new row's id, or None in DRY_RUN mode.
        """
        assert self.conn is not None
        if self.data_source_id is None:
            raise RuntimeError("Call open_run() before insert()")

        data = {**data, "data_source_id": self.data_source_id}
        cols = list(data.keys())
        placeholders = ", ".join(["%s"] * len(cols))
        col_list = ", ".join(cols)

        if DRY_RUN:
            print(f"[DRY_RUN] INSERT INTO {table} ({col_list}) VALUES {tuple(data.values())}")
            self.rows_skipped += 1
            return None

        row = self.conn.execute(
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) RETURNING id",
            list(data.values()),
        ).fetchone()
        self.rows_written += 1
        assert row is not None
        return row["id"]

    def bulk_insert(self, table: str, rows: list[dict[str, Any]]) -> int:
        """
        Insert many rows in a single round trip using psycopg3 pipelined executemany.

        All rows must share the same column set. data_source_id is auto-attached
        to every row if not already present. Returns the count of rows inserted.

        Use this whenever an IngestJob writes >50 rows of the same shape — it's
        ~50-100x faster than calling self.insert() in a loop because the trip
        latency across the public Railway URL no longer dominates.
        """
        assert self.conn is not None
        if not rows:
            return 0
        if self.data_source_id is None:
            raise RuntimeError("Call open_run() before bulk_insert()")

        # Ensure every row has data_source_id
        for row in rows:
            row.setdefault("data_source_id", self.data_source_id)

        # Use the union of keys (in stable order) so caller can omit nullable cols
        cols: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for k in row.keys():
                if k not in seen:
                    seen.add(k)
                    cols.append(k)

        placeholders = ", ".join(["%s"] * len(cols))
        col_list = ", ".join(cols)

        if DRY_RUN:
            print(f"[DRY_RUN] BULK INSERT {table} ({col_list}) × {len(rows)} rows")
            self.rows_skipped += len(rows)
            return 0

        values = [tuple(r.get(c) for c in cols) for r in rows]
        with self.conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})",
                values,
            )
        self.rows_written += len(rows)
        return len(rows)

    def upsert(self, table: str, data: dict[str, Any], conflict_cols: list[str]) -> int | None:
        """
        Insert with ON CONFLICT DO UPDATE. Useful for idempotent re-runs.
        Returns the id of the existing or newly created row.
        """
        assert self.conn is not None
        if self.data_source_id is None:
            raise RuntimeError("Call open_run() before upsert()")

        data = {**data, "data_source_id": self.data_source_id}
        cols = list(data.keys())
        placeholders = ", ".join(["%s"] * len(cols))
        col_list = ", ".join(cols)
        update_cols = [c for c in cols if c not in conflict_cols]
        update_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
        conflict_list = ", ".join(conflict_cols)

        if DRY_RUN:
            print(f"[DRY_RUN] UPSERT {table} ON ({conflict_list}) {data}")
            self.rows_skipped += 1
            return None

        row = self.conn.execute(
            f"""
            INSERT INTO {table} ({col_list}) VALUES ({placeholders})
            ON CONFLICT ({conflict_list}) DO UPDATE SET {update_clause}
            RETURNING id
            """,
            list(data.values()),
        ).fetchone()
        self.rows_written += 1
        assert row is not None
        return row["id"]

    # -- orchestrator --------------------------------------------------------

    @abstractmethod
    def ingest(self) -> None:
        """Subclass: do the actual ingest work. Use self.insert/self.upsert."""
        ...

    def run(self) -> dict[str, Any]:
        """Top-level entry. Opens connection, creates data_source row, runs ingest."""
        with connect() as conn:
            self.conn = conn
            self.open_run()
            self.ingest()
            self.close_run()
            return {
                "run_id": self.run_id,
                "data_source_id": self.data_source_id,
                "rows_written": self.rows_written,
                "rows_skipped": self.rows_skipped,
            }
