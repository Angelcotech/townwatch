from contextlib import contextmanager
from typing import Iterator
import psycopg
from psycopg.rows import dict_row

from .config import DATABASE_URL
from .resilience import retry_transient


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    """Open a Postgres connection with dict rows. Commits on success, rolls back
    on error.

    Connection ESTABLISHMENT is wrapped in retry_transient: at scale the rate of
    fresh DNS lookups can overwhelm the OS resolver (EAI_NONAME) and the proxy
    can briefly refuse connections. These are momentary, so a backoff-retry here
    makes every job that opens a connection (~20 of them) resilient to resolver
    hiccups instead of failing. Mid-session drops are handled one layer up, by
    the job loops re-running their unit of work."""
    conn = retry_transient(
        lambda: psycopg.connect(DATABASE_URL, row_factory=dict_row),
        label="db.connect",
    )
    with conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
