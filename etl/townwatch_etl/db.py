from contextlib import contextmanager
from typing import Iterator
import psycopg
from psycopg.rows import dict_row

from .config import DATABASE_URL


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    """Open a Postgres connection with dict rows. Commits on success, rolls back on error."""
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
