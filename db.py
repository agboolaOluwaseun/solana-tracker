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

_local = threading.local()


def _row_factory(cursor: sqlite3.Cursor, row: tuple) -> dict:
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def get_connection() -> sqlite3.Connection:
    """Return a thread-local connection. Connections use row dicts and FK enforcement."""
    conn = _local.__dict__.get("conn")
    if conn is None:
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(settings.db_path, isolation_level=None,
                               timeout=30)  # autocommit; generous busy-wait:
        # WAL write-write collisions across processes (scan vs boot rescore vs
        # refresh stream) should wait for the other writer, not raise.
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
    """Create tables if they don't exist (schema chosen by settings.schema_file)."""
    schema_path = PROJECT_ROOT / settings.schema_file
    schema = schema_path.read_text(encoding="utf-8")
    conn = get_connection()
    conn.executescript(schema)
    # Guarded migration for pre-existing DBs: CREATE TABLE IF NOT EXISTS does
    # not add columns to a table that already exists.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(calls)").fetchall()}
    if "pending_reason" not in cols:
        conn.execute("ALTER TABLE calls ADD COLUMN pending_reason TEXT")
    if "score_state" not in cols:
        # Live rescoring: 'live' = provisional verdict inside the open 7d
        # window (rescored every pass), 'final' = window elapsed or legacy.
        conn.execute("ALTER TABLE calls ADD COLUMN score_state TEXT NOT NULL DEFAULT 'final'")
    # Guarded migration for the 'expired' convention (user rule 2026-09):
    # the stop strategies record their mark-to-market so gray tiles can show
    # "out @ X" without re-walking candles.
    sl_cols = {r["name"] for r in conn.execute("PRAGMA table_info(stoploss_results)").fetchall()}
    if sl_cols and "final_close_usd" not in sl_cols:
        conn.execute("ALTER TABLE stoploss_results ADD COLUMN final_close_usd REAL")
        conn.execute("ALTER TABLE stoploss_results ADD COLUMN final_close_ts TEXT")
    # Pool-orientation guard (museic/DRUGS lesson 2026-09): remember, per
    # cached token, the paired quote symbol and whether the called token
    # holds GeckoTerminal's base seat (the only state in which GT's curve
    # prices the RIGHT asset). Checked once per suspect pool; cached forever.
    tm_cols = {r["name"] for r in conn.execute("PRAGMA table_info(token_meta)").fetchall()}
    if tm_cols and "quote_symbol" not in tm_cols:
        conn.execute("ALTER TABLE token_meta ADD COLUMN quote_symbol TEXT")
        conn.execute("ALTER TABLE token_meta ADD COLUMN pool_is_base INTEGER")
    # Scan checkpoint on channels (opportunistic refresh anchor). Seeded in
    # the same guarded migration: started_at of the latest completed run is
    # the newest point up to which that run's FETCH phase walked messages
    # (pricing happens after the scan, so started_at never over-promises
    # coverage). NULL = never scanned; refresh falls back to the old anchor.
    ch_cols = {r["name"] for r in conn.execute("PRAGMA table_info(channels)").fetchall()}
    if "last_scanned_at" not in ch_cols:
        conn.execute("ALTER TABLE channels ADD COLUMN last_scanned_at TEXT")
        try:
            conn.execute("""
                UPDATE channels SET last_scanned_at = (
                    SELECT MAX(r.started_at) FROM ingestion_runs r
                    WHERE r.channel_id = channels.id AND r.status = 'completed'
                )
                WHERE last_scanned_at IS NULL
            """)
        except sqlite3.OperationalError:
            pass  # legacy DB without ingestion_runs — fallback anchor applies
