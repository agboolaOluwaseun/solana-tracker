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


def test_refresh_and_fetch_both_allow_birdeye_rescue():
    """Policy ruling 2026-10-01 (user: 'fix that'): boot/refresh SCAN is
    each new call's INITIAL scan — GT-blind fresh mints must be rescued by
    Birdeye there, not left to rot in waiting_for_data until a manual
    Fetch. The API-on-refresh ban lives on in the RESCORE lane (its
    price_one_call call sites pass no allow_birdeye — default closed;
    saved candles come from the token-primary cache)."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "api" / "server.py").read_text()
    refresh = src.split("async def _one_channel")[1].split("async def event_generator")[0]
    assert "birdeye_rescue=True" in refresh


def test_zombie_run_rotates_instead_of_resuming(gate_env, monkeypatch):
    """The Sep-29 launch-staleness bug: a run interrupted for SIX DAYS
    must NOT resume — its completed-channel list is historical and would
    fold every channel as 'done', scanning nothing. Older than
    BOOT_RUN_MAX_AGE, mark_boot_started rotates a fresh token and clears
    the done list."""
    t = BR.mark_boot_started()
    BR.mark_channel_done(t, 101)
    BR.mark_channel_done(t, 102)
    # interrupted: no mark_boot_finished
    _advance(monkeypatch, days=6)
    due, token = BR.boot_refresh_due()
    assert due and token == t                       # still the old token seen
    t2 = BR.mark_boot_started()
    assert t2 != t                                  # ROTATED, not resumed
    assert BR.completed_channel_ids(t2) == set()    # done list cleared
    # and the fresh run must NOT lose its own resume behaviour:
    BR.mark_channel_done(t2, 101)
    _advance(monkeypatch, minutes=4)
    assert BR.mark_boot_started() == t2             # young interrupt resumes


def test_zombie_boundary_resumes_within_age(gate_env, monkeypatch):
    """An interrupted run YOUNGER than BOOT_RUN_MAX_AGE still resumes
    (genuine same-day crash recovery — the original feature)."""
    t = BR.mark_boot_started()
    BR.mark_channel_done(t, 101)
    _advance(monkeypatch, hours=23)
    assert BR.mark_boot_started() == t


def test_running_age_distinguishes_inflight_from_complete(gate_env, monkeypatch):
    """The reopen-lie fix (2026-09-30 offline boot): the server's skip
    message must know an in-flight run from a completed day. running_age_s
    returns the live run's age, None once closed."""
    t = BR.mark_boot_started()
    assert BR.running_age_s() is not None            # in flight
    assert BR.hours_since_last_finished() is None    # nothing completed yet
    BR.mark_boot_finished(t)
    assert BR.running_age_s() is None                # closed
    assert BR.hours_since_last_finished() < 1/3600  # just completed


GEPPETTO_REAL = ("fetch failed: ChatIdInvalidError: Invalid object ID for a "
    "chat. Make sure to pass the right types, for instance making sure that "
    "the request is designed for chats (not channels/megagroups) or otherwise "
    "look for a different one more suited (caused by GetChatsRequest)")

def test_permanent_scan_error_classification():
    """Geppetto loop (2026-10-01): one unreachable channel vetoed the daily
    gate close forever — every open resumed the run and replayed rescore."""
    assert BR.is_permanent_scan_error(GEPPETTO_REAL)
    assert BR.is_permanent_scan_error(
        "fetch failed: ValueError: Could not find the input entity for "
        "PeerChannel(channel_id=701473573202)")
    assert BR.is_permanent_scan_error(
        "fetch failed: UsernameNotPresentError: (error 190)")
    # transient errors must stay retryable:
    assert not BR.is_permanent_scan_error(
        "fetch failed: ConnectionTimeout")
    assert not BR.is_permanent_scan_error(
        "fetch failed: FloodWaitError: 300 seconds")
    assert not BR.is_permanent_scan_error(None)


def test_unreachable_channel_does_not_veto_gate_close():
    """Dispatch-level: 'unreachable' marks the channel done and does NOT
    set had_error, so the run closes cleanly and tomorrow rotates fresh."""
    src = open("api/server.py").read()
    assert "BR.is_permanent_scan_error" in src
    assert "out[0] = 'unreachable'" in src
    assert 'out2[0] in (\'done\', \'unreachable\')' in src
    assert "elif out[0] == 'error':\n                    had_error = True" in src


def test_transient_error_parks_channel_until_network_returns():
    """User ruling 2026-10-02: an offline mid-run must NOT race the queue
    firing per-channel errors — the refresh stays ON the channel that lost
    the network, retrying (bounded 30-min budget), and auto-continues when
    connectivity returns. Permanent failures return 'unreachable' (never
    'error'), so everything parked is retryable by definition. Gate stays
    open while parked (no done-mark before the wait ends), so closing the
    tab resumes from exactly this channel — the durable half."""
    from pathlib import Path
    gen = (Path(__file__).resolve().parents[1] / "api" / "server.py").read_text()
    assert "NET_RETRY_BUDGET_S = 1800" in gen
    assert "while out[0] == 'error' and waited_s < NET_RETRY_BUDGET_S" in gen
    assert "network down — waiting here, auto-retry" in gen
    # the retry re-runs the SAME channel row, then falls through the normal
    # done/error dispatch (had_error untouched on recovery -> gate can close)
    assert "async for chunk in _one_channel(row, out, opportunistic=True)" in gen
    assert gen.count("async for chunk in _one_channel(row, out, opportunistic=True)") == 2


def test_startup_rescore_defers_to_visible_stream():
    """Visibility ruling (user 2026-10-06): the lifespan background catch-up
    must NOT run rescore_live_calls while the daily boot refresh is still
    DUE — the stream that's about to open narrates that exact pass with its
    progress counter; an invisible holder of the single-flight guard makes
    the visible phase flash by at 0/0. It still runs when the gate is
    already closed (server restart after the day's scan — nobody watching,
    verdicts must mature anyway)."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "api" / "server.py").read_text()
    life = src.split("async def _lifespan")[1].split("_threading.Thread")[0]
    assert "if not due:" in life
    assert "rescore_live_calls()" in life.split("if not due:")[1]
    assert "BR.boot_refresh_due()" in life
    # mature_pending_calls stays unconditional (cheap DB pass, no counter)
    assert "mature_pending_calls()" in life


def test_interrupted_fetch_queue_resume():
    """User rule 2026-10-10: a fetch interrupted mid-stream (client gone,
    server restart) must RESUME as visible cards before the usual startup
    update. Server contract: queue rows registered at stream start; the
    endpoint lists them; rows are consumed per-channel as their turn
    starts; headless fallback replays only rows older than 10 min so a
    reopening frontend never double-runs a fresh one."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "api" / "server.py").read_text()
    assert "INSERT OR REPLACE INTO fetch_queue" in src
    assert "CREATE TABLE IF NOT EXISTS fetch_queue" in (Path(__file__).resolve().parents[1] / "db.py").read_text()
    # endpoint + route
    assert "/api/pending-fetches" in src
    assert "Route(\"/api/pending-fetches\", pending_fetches)" in src
    # stream registers the whole list up front, consumes per-channel start
    assert "_fetch_queue_add(_r[\"id\"], days)" in src
    assert "_fetch_queue_drop(row[\"id\"])" in src
    # lifespan headless fallback (stale rows only) before startup catch-up
    life = src.split("async def _lifespan")[1].split("_threading.Thread")[0]
    assert "resume_fetch_queue_sync(max_age_minutes=10)" in life
    assert "mature_pending_calls()" in life
    # resume outranks the refresh (yield flag) like a user fetch
    helper_src = src.split("def resume_fetch_queue_sync")[1].split("def resume")[0] if "def resume_fetch_queue_sync" in src else ""
    assert "request_telegram_yield(True)" in helper_src
