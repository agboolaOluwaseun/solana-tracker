"""Telegram guard: user fetches never wait for opportunistic refreshes.

The contract (fetchStore concurrency rework):
  * run_backfill(opportunistic=True) returns stage='yielded' WITHOUT
    persisting calls while a user fetch has raised the yield flag.
  * A normal run_backfill is unaffected by the flag.
  * fetch_window with opportunistic=False blocks on the session lock
    instead of yielding (the retry lane semantics).
"""
import dataclasses
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config
import db as dbmod
from ingestion import telegram_guard as guard
from ingestion.telegram_guard import TelegramYield, request_telegram_yield


def _scratch_db(tmp_path, monkeypatch):
    """Point config/pipeline/db at a fresh unified-schema DB (same pattern
    as tests/test_live_scoring.py::live_env)."""
    import pipeline

    db_path = tmp_path / "guard.db"
    conn = sqlite3.connect(db_path)
    conn.executescript((config.PROJECT_ROOT / "schema_unified.sql").read_text())
    conn.commit()
    conn.close()

    old = dbmod._local.__dict__.get("conn")
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
        dbmod._local.__dict__.pop("conn", None)

    new_settings = dataclasses.replace(
        config.settings, db_path=db_path, schema_file="schema_unified.sql")
    monkeypatch.setattr(config, "settings", new_settings)
    monkeypatch.setattr(pipeline, "settings", new_settings)
    monkeypatch.setattr(dbmod, "settings", new_settings)
    import pricing.backtest as _bt
    monkeypatch.setattr(_bt, "settings", new_settings, raising=False)
    return db_path


def test_yield_flag_roundtrip():
    request_telegram_yield(True)
    assert guard.should_yield_fetch()
    request_telegram_yield(False)
    assert not guard.should_yield_fetch()


def test_backfill_yielded_path(tmp_path, monkeypatch):
    """An opportunistic run that hits TelegramYield returns early with
    stage='yielded' and a 'retry' narration — and persists no calls."""
    import pipeline

    _scratch_db(tmp_path, monkeypatch)

    def exploding_fetch(ref, ws, we, limit=None, progress_cb=None,
                        opportunistic=False):
        assert opportunistic, "run_backfill must pass the flag through"
        raise TelegramYield(ref)

    monkeypatch.setattr(pipeline, "_import_fetch_window_sync",
                        lambda: exploding_fetch)
    request_telegram_yield(True)
    try:
        prog = pipeline.run_backfill(
            channel_ref="@guardtest",
            window_start=datetime(2026, 9, 1),
            window_end=datetime(2026, 9, 15),
            title="Guard Test",
            progress_cb=lambda p: None,
            opportunistic=True,
        )
    finally:
        request_telegram_yield(False)

    assert prog.stage == "yielded"
    assert "yielded to user fetch" in prog.message
    conn = pipeline.get_connection()
    n = conn.execute("SELECT COUNT(*) c FROM calls").fetchone()["c"]
    assert n == 0, "a yielded run must persist nothing"


def test_non_opportunistic_unaffected(tmp_path, monkeypatch):
    """A user fetch (opportunistic=False) ignores the yield flag entirely —
    it still raises the flag for refreshes, but its own scan runs normally."""
    import pipeline
    from models import RawMessage

    _scratch_db(tmp_path, monkeypatch)

    calls = []

    def quiet_fetch(ref, ws, we, limit=None, progress_cb=None,
                    opportunistic=False):
        calls.append(opportunistic)
        # A message with no token address -> run proceeds to 'no calls'.
        return ([RawMessage(channel_id=5, message_id=1, text="gm fam",
                            timestamp=ws)], "Quiet")

    monkeypatch.setattr(pipeline, "_import_fetch_window_sync",
                        lambda: quiet_fetch)
    request_telegram_yield(True)
    try:
        prog = pipeline.run_backfill(
            channel_ref="@quiet", window_start=datetime(2026, 9, 1),
            window_end=datetime(2026, 9, 15), title="Quiet",
            progress_cb=lambda p: None, opportunistic=False,
        )
    finally:
        request_telegram_yield(False)

    assert calls == [False]
    assert prog.stage == "done"
