"""Connection helper for the invoice reconciliation database.

A thin wrapper over stdlib ``sqlite3`` — no ORM. Every connection this
module opens enforces foreign keys, which SQLite does not do by default.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection to the database at ``db_path`` with foreign keys on.

    ``db_path`` may be a plain path or ``":memory:"``. Row access is by
    name (``sqlite3.Row``) so callers can read columns by key.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Context manager yielding a connection, closed on exit.

    Commits on clean exit, rolls back and re-raises on exception.
    """
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
