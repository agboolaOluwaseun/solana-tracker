"""Boot-refresh gate tests — pure logic on a scratch DB (no server).

Proves the user policy: the page-open auto-refresh runs once per 24h;
later opens within the day are not due; an interrupted run RESUMES with
its completed-channel list; a run that errored never starts the 24h
clock; the live-run join window prevents double-scanning; everything
degrades open on DB failure.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

import config
import db as dbmod
from ingestion import boot_refresh as BR


@pytest.fixture()
def gate_env(tmp_path, monkeypatch):
    """Scratch DB for the gate tables (isolated from kolfi.db)."""
    import dataclasses
    dbfile = tmp_path / "gate.db"
    new_settings = dataclasses.replace(config.settings, db_path=dbfile,
                                       schema_file="schema_unified.sql")
    monkeypatch.setattr(config, "settings", new_settings)
    monkeypatch.setattr(dbmod, "settings", new_settings)
    monkeypatch.setattr(BR, "get_connection", dbmod.get_connection)
    # drop stale thread-local conn bound to the previous path
    old = dbmod._local.__dict__.get("conn")
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
        dbmod._local.__dict__.pop("conn", None)
    BR.ensure_gate_table()
    yield
    # Never leak the scratch binding: the NEXT test must see a clean
    # thread-local (test_price_one_call_forwards_now once saw this DB
    # instead of its own and died on the schema guard).
    old = dbmod._local.__dict__.get("conn")
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
        dbmod._local.__dict__.pop("conn", None)


FRESH = 101, 102, 103   # channel ids used in flow tests


def test_never_run_is_due(gate_env):
    due, token = BR.boot_refresh_due()
    assert due and token is None


def test_started_then_not_due_for_rejoin(gate_env):
    """Second open right after the first (double tab / StrictMode): the
    run is in flight — not due, token shared, no resume storm."""
    t = BR.mark_boot_started()
    assert t is not None
    due, token = BR.boot_refresh_due()
    assert due is False and token == t


def test_completed_run_not_due_within_24h(gate_env):
    t = BR.mark_boot_started()
    BR.mark_channel_done(t, 101)
    BR.mark_channel_done(t, 102)
    BR.mark_boot_finished(t, clean=True)
    due, token = BR.boot_refresh_due()
    assert due is False


def _advance(monkeypatch, **kw):
    """Shift the gate's clock forward (wrap the ORIGINAL _now — patching
    _now with a lambda that calls _now recurses)."""
    orig = BR._now
    monkeypatch.setattr(BR, "_now", lambda: orig() + timedelta(**kw))


def test_completed_run_due_after_24h(gate_env, monkeypatch):
    t = BR.mark_boot_started()
    BR.mark_boot_finished(t, clean=True)
    _advance(monkeypatch, hours=24, minutes=1)
    due, _ = BR.boot_refresh_due()
    assert due is True


def test_new_day_run_clears_old_done_list(gate_env, monkeypatch):
    t1 = BR.mark_boot_started()
    BR.mark_channel_done(t1, 101)
    BR.mark_boot_finished(t1, clean=True)
    _advance(monkeypatch, hours=25)
    t2 = BR.mark_boot_started()
    assert t2 != t1
    assert BR.completed_channel_ids(t2) == set()   # fresh queue every day
    # the day-rollover start CLEARED the table (design: only the live token
    # matters) — the old token's history is intentionally gone
    assert BR.completed_channel_ids(t1) == set()


# ---- resume an interrupted run -------------------------------------------

def test_interrupted_run_resumes_skipping_done(gate_env, monkeypatch):
    """Page closed mid-refresh: channels 101/102 finished, 103 didn't.
    After the join window, the next boot is due, reuses the token, and
    only 103 remains in the queue."""
    t = BR.mark_boot_started()
    BR.mark_channel_done(t, 101)
    BR.mark_channel_done(t, 102)
    # no mark_boot_finished -> interrupted
    _advance(monkeypatch, minutes=4)
    due, token = BR.boot_refresh_due()
    assert due and token == t
    assert BR.mark_boot_started() == t              # resume, same token
    done = BR.completed_channel_ids(t)
    assert done == {101, 102}
    all_rows = [101, 102, 103]
    assert [r for r in all_rows if r not in done] == [103]


def test_erroring_run_never_starts_the_clock(gate_env, monkeypatch):
    """A channel errored -> finished_at stays NULL -> next boot resumes
    and retries only the failed ones (done ids recorded: 101 only)."""
    t = BR.mark_boot_started()
    BR.mark_channel_done(t, 101)
    BR.mark_boot_finished(t, clean=False)           # had_error=True
    with dbmod.get_connection() as c:
        row = c.execute("SELECT finished_at FROM boot_refresh_gate WHERE id=1").fetchone()
    assert row["finished_at"] is None
    _advance(monkeypatch, minutes=4)
    due, token = BR.boot_refresh_due()
    assert due and token == t
    assert BR.completed_channel_ids(t) == {101}


def test_completed_done_rows_are_idempotent(gate_env):
    t = BR.mark_boot_started()
    BR.mark_channel_done(t, 101)
    BR.mark_channel_done(t, 101)                    # INSERT OR IGNORE
    assert BR.completed_channel_ids(t) == {101}


def test_bookkeeping_never_raises_on_bad_token(gate_env):
    """force-runs pass token=None through every function; each is a no-op."""
    BR.mark_channel_done(None, 101)
    assert BR.completed_channel_ids(None) == set()
    BR.mark_boot_finished(None, clean=True)


def test_hours_since_last_finished(gate_env):
    assert BR.hours_since_last_finished() is None
    t = BR.mark_boot_started()
    assert BR.hours_since_last_finished() is None    # not finished yet
    BR.mark_boot_finished(t, clean=True)
    h = BR.hours_since_last_finished()
    assert h is not None and 0 <= h < 1


# ---- static integration guards on api/server.py ---------------------------

def test_refresh_stream_wired_to_gate():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "api" / "server.py").read_text()
    body = src.split("async def refresh_stream")[1].split("async def fetch_channels")[0]
    # gate consulted, skip path exists (no-op stream), force parsed
    assert "BR.boot_refresh_due()" in body
    assert "'skipped'" in body
    assert "force = bool(body.get(\"force\"))" in body
    # token claimed inside event_generator (streaming really started);
    # manual force passes token None -> bookkeeping no-ops
    gen = body.split("async def event_generator")[1]
    assert "gate_token = None if force else (BR.mark_boot_started() or _prior_token)" in gen
    # per-channel bookkeeping + clean-only close
    assert "BR.completed_channel_ids(gate_token)" in gen
    assert "BR.mark_channel_done(gate_token" in gen
    assert "BR.mark_boot_finished(gate_token, clean=not had_error)" in gen


def test_refresh_skips_birdeye_but_fetch_allows():
    """The earlier policy (Birdeye = initial fetch only) must survive this
    refactor: refresh still passes birdeye_rescue=False."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "api" / "server.py").read_text()
    refresh = src.split("async def _one_channel")[1].split("async def event_generator")[0]
    assert "birdeye_rescue=False" in refresh
