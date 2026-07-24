"""
SQLite access layer.

Provides a module-level connection (thread-local so Streamlit's reruns are safe)
and an explicit init function that applies schema.sql.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from config import PROJECT_ROOT, settings

_SCHEMA_PATH = PROJECT_ROOT / "schema.sql"
_local = threading.local()


def _row_factory(cursor: sqlite3.Cursor, row: tuple) -> dict:
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def get_connection() -> sqlite3.Connection:
    """Return a thread-local connection. Connections use row dicts and FK enforcement."""
    conn = _local.__dict__.get("conn")
    if conn is None:
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(settings.db_path, isolation_level=None)  # autocommit
        conn.row_factory = _row_factory
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        _local.conn = conn
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Run a transaction explicitly. Commits on success, rolls back on error."""
    conn = get_connection()
    conn.execute("BEGIN;")
    try:
        yield conn
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise


def init_db() -> None:
    """Create tables if they don't exist."""
    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn = get_connection()
    conn.executescript(schema)
