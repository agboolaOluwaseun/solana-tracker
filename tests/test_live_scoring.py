"""Live (provisional) scoring tests — window truncation + rescore lifecycle.

Two layers:
  1. Engine: evaluate_call_7d(..., now=...) against synthetic fetchers.
     No DB, no network.
  2. Pipeline: run_backfill + rescore_live_calls on a scratch DB with the
     fetcher and price_one_call faked — proves score_state transitions, the
     waiting retry lane, the loss->win flip as the window extends, finalization
     at day 7, and idempotence. Zero live API calls.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

import config
import db as dbmod
from models import BacktestResult, ParsedCall, PricePoint, StoplossResult
from pricing.strategy7d import evaluate_call_7d

CALL_TS = datetime(2026, 4, 1, 12, 34, 0)
HOUR_START = datetime(2026, 4, 1, 12, 0, 0)
POOL = "poolLIVE"
TOKEN = "toke…mp"


def hourly(ts, h, l):
    return PricePoint(timestamp=ts, open=l, high=h, low=l, close=h, volume=0.0)


def minute(ts, o, h, l):
    return PricePoint(timestamp=ts, open=o, high=h, low=l, close=o, volume=0.0)


def make_fetchers(hourly_candles, minute_candles):
    def fetch_hourly(pool, token, start, end):
        out = sorted((c for c in hourly_candles if start <= c.timestamp < end),
                     key=lambda c: c.timestamp)
        return out, 1

    def fetch_minute(pool, token, start, end):
        out = sorted((m for m in minute_candles if start <= m.timestamp < end),
                     key=lambda m: m.timestamp)
        return out, 1

    return fetch_hourly, fetch_minute


def evaluate(hs, ms, now=None):
    fh, fm = make_fetchers(hs, ms)
    return evaluate_call_7d(POOL, TOKEN, CALL_TS, fh, fm, now=now)


# ---- 1. Engine live mode --------------------------------------------------

def test_frozen_default_unchanged():
    """No `now` => classic final screening loss, window_complete=True."""
    hs = [hourly(HOUR_START, 1.5, 1.0)] + [
        hourly(HOUR_START + timedelta(hours=i + 1), 1.4, 0.9)
        for i in range(24 * 7)
    ]
    r = evaluate(hs, [])
    assert r.window_complete is True
    assert r.status_plain == "loss"
    assert r.note == "screening LOSS"


def test_live_screening_miss_is_provisional_loss():
    """2 days observed, never reached 2x => loss status, window still open."""
    hs = [hourly(HOUR_START, 1.5, 1.0)] + [
        hourly(HOUR_START + timedelta(hours=i + 1), 1.4, 0.9) for i in range(48)
    ]
    r = evaluate(hs, [], now=CALL_TS + timedelta(days=2))
    assert r.window_complete is False
    assert r.status_plain == "loss"          # provisional
    assert "provisional" in r.note
    assert r.api_requests_used == 1          # screening keeps the cheap path


def test_live_win_immediate_but_window_open():
    """2x reached => win recorded instantly, stats can still grow."""
    hs = [hourly(HOUR_START, 1.5, 1.0),
          hourly(HOUR_START + timedelta(hours=3), 2.2, 1.0)]
    ms = [minute(CALL_TS, 1.0, 2.1, 1.0)]
    r = evaluate(hs, ms, now=CALL_TS + timedelta(hours=6))
    assert r.window_complete is False
    assert r.status_plain == "win"
    assert r.which_threshold_first == "2x"


def test_live_window_end_truncates_leaky_fetcher():
    """Candles past `now` must not contaminate the provisional verdict."""
    hs = [hourly(HOUR_START, 1.5, 1.0)]
    hs += [hourly(HOUR_START + timedelta(hours=1 + i), 1.4, 0.9)
           for i in range(10)]
    hs.append(hourly(HOUR_START + timedelta(hours=120), 3.0, 0.9))  # day-5 pump
    r = evaluate(hs, [], now=CALL_TS + timedelta(days=2))
    # screening over [call, now) ignores the day-5 candle -> provisional loss
    assert r.window_complete is False and r.status_plain == "loss"


def test_live_minus50_stoploss_loss_plain_open():
    """-50% before any 2x in the observed slice: both strategies 'loss',
    window open. Screening passes only via the call-hour high (discarded
    from the path), so the minute walk — not screening — decides here."""
    hs = [hourly(HOUR_START, 2.5, 1.0),                     # screening passes
          hourly(HOUR_START + timedelta(hours=1), 1.4, 0.9),
          hourly(HOUR_START + timedelta(hours=2), 1.4, 0.9),
          hourly(HOUR_START + timedelta(hours=3), 1.4, 0.9)]
    ms = [minute(CALL_TS, 1.0, 1.0, 1.0),
          minute(CALL_TS + timedelta(minutes=10), 1.0, 1.0, 0.45)]
    r = evaluate(hs, ms, now=CALL_TS + timedelta(hours=4))
    assert r.window_complete is False
    assert r.status_stoploss == "loss"
    assert r.status_plain == "loss"        # 2x never seen yet... provisional
    assert r.minus_50_reached and not r.target_2x_reached
    assert r.which_threshold_first == "minus50"


# ---- 1b. price_one_call must FORWARD now to the engine ---------------------
# Regression guard: an earlier integration pass implemented live mode at the
# loop level but price_one_call silently dropped `now` — every 'live' verdict
# came back window_complete=True and finalized young calls instantly. Faking
# price_one_call itself could never catch that; this test runs the REAL
# price_one_call body with only the client boundary faked (pool lookup,
# GT v2 client, DexScreener).

def _candles_for_screening_and_walk():
    """Call-hour low 1.0 (screening target 2.0). Day-2 hourly pumps to 2.2.
    Full window => win; window truncated to day 1 => provisional loss."""
    hs = [hourly(HOUR_START, 2.5, 1.0),                       # call hour
          hourly(HOUR_START + timedelta(hours=24), 2.2, 1.0)]  # day-2 pump
    return hs


def test_price_one_call_forwards_now(monkeypatch):
    """now inside window -> engine sees truncated window (window_complete False)."""
    import pipeline
    from pricing import backtest as bt
    from models import PricePoint  # noqa: F401  (used by fetcher shapes)

    hs = _candles_for_screening_and_walk()
    ms = [minute(CALL_TS, 1.0, 1.0, 1.0)]

    def fake_cached_fetchers(conn, client_v2):
        return make_fetchers(hs, ms)

    monkeypatch.setattr("pricing.strategy7d_cache.make_cached_fetchers",
                        fake_cached_fetchers, raising=True)
    # Bypass pool resolution entirely: pretend token_meta already has a pool.
    monkeypatch.setattr(bt, "get_cached_pool",
                        lambda addr, chain="sol": {"token_address": addr,
                                                   "pool_address": POOL,
                                                   "symbol": "T", "name": None,
                                                   "liquidity_usd": None})
    monkeypatch.setattr(pipeline, "_get_v2_client", lambda chain: object())
    monkeypatch.setattr(pipeline, "_get_ds_client", lambda: None)

    # Young call, live mode: the day-2 pump (24h after hour start) is BEYOND
    # now (23h after the call) => provisional loss.
    br, sl, r7 = pipeline.price_one_call("sol", None, TOKEN, CALL_TS,
                                         now=CALL_TS + timedelta(hours=23))
    assert r7 is not None
    assert r7.window_complete is False
    assert r7.status_plain == "loss"
    assert pipeline.score_state_7d(r7) == "live"

    # Same data, window fully elapsed (now past day 2) => final win. The only
    # difference is the truncation point — proves `now` reaches the engine.
    br2, sl2, r7b = pipeline.price_one_call("sol", None, TOKEN, CALL_TS,
                                            now=CALL_TS + timedelta(days=7, hours=1))
    assert r7b is not None
    assert r7b.window_complete is True and r7b.status_plain == "win"
    assert pipeline.score_state_7d(r7b) == "final"

    # now=None (frozen spec path) must stay full-window too.
    _, _, r7c = pipeline.price_one_call("sol", None, TOKEN, CALL_TS)
    assert r7c.window_complete is True and r7c.status_plain == "win"


# ---- 1c. Cache fetchers: dead-pool pruning rescue (user scenario) ---------
# Token pool visible for ~3 days, then GeckoTerminal stops serving its
# history (delisted/dead). Live passes captured candles into price_cache as
# they appeared; the day-7 finalization must score from everything EVER
# captured, not collapse because the API no longer returns it.

def test_fetchers_survive_api_pruning(tmp_path):
    import sqlite3 as _sq

    from pricing.strategy7d_cache import make_cached_fetchers

    db = tmp_path / "c.db"
    conn = _sq.connect(db)
    conn.executescript("""
        CREATE TABLE price_cache (
            pool_address TEXT NOT NULL, token_address TEXT NOT NULL,
            aggregate TEXT NOT NULL, candle_ts TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (pool_address, token_address, aggregate, candle_ts)
        );
    """)

    # Full observed window: quiet days 0-1, 2x pump at hour 30.
    full = [hourly(HOUR_START + timedelta(hours=k),
                   2.2 if k == 30 else 1.4,
                   1.0 if k == 30 else 0.9) for k in range(72)]

    class FakeGT:
        network = "solana"
        def __init__(self):
            self.mode = "full"      # -> "pruned" -> "gone"
        def _request(self, path, params):
            if self.mode == "gone":
                raise RuntimeError("404 Pool not found")
            # GT returns the LAST 1000 candles before before_timestamp;
            # 'pruned' simulates the API dropping old history after 24h.
            src = full if self.mode == "full" else [c for c in full
                                                    if c.timestamp >= HOUR_START + timedelta(hours=24)]
            return {"data": {"attributes": {"ohlcv_list": [
                [int(c.timestamp.timestamp()), c.open, c.high, c.low, c.close, c.volume]
                for c in src]}}}

    gt = FakeGT()
    fetch_hourly, _ = make_cached_fetchers(conn, gt)
    end7 = CALL_TS + timedelta(days=7)

    # Day-2 live pass: captures the quiet start + (not yet) the pump.
    day2 = [c for c in full if c.timestamp < HOUR_START + timedelta(hours=24)]
    pruned_src_backup = list(full)
    del full[:]
    full.extend(day2)
    hs, _reqs = fetch_hourly(POOL, TOKEN, HOUR_START, CALL_TS + timedelta(days=2))
    assert any(c.timestamp == HOUR_START for c in hs)      # screening data ok
    # Days 2-3 pass captures the pump BEFORE the API prunes history.
    del full[:]
    full.extend(pruned_src_backup)
    hs2, _ = fetch_hourly(POOL, TOKEN, HOUR_START, CALL_TS + timedelta(days=3))
    pump = [c for c in hs2 if c.high >= 2.0]
    assert pump, "pump hour captured live"

    # Pool now deleted: API returns 404 for everything.
    del full[:]
    full.extend(pruned_src_backup)
    gt.mode = "gone"
    hs3, _ = fetch_hourly(POOL, TOKEN, HOUR_START, end7)
    # Day-7 finalization: rescue entirely from cache — call-hour present and
    # the day-3 pump still visible even though the API has nothing left.
    assert any(c.timestamp == HOUR_START for c in hs3), "call-hour from cache"
    assert any(c.high >= 2.0 for c in hs3), "pump from cache survives pruning"
    conn.close()


# ---- 2. Pipeline lifecycle (scratch DB, faked fetch + pricing) ------------

SOL = "3TYgKwkE2Y3rxdw9osLRSpxpXmSC1C1oo19W9KHspump"
WAIT_ADDR = "waaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1pump"


class FakeEval:
    def __init__(self, status="win", complete=True, note="2x",
                 pool=None, end7=None):
        self.status_plain = status
        self.status_stoploss = status
        self.window_complete = complete
        self.note = note
        self.pool_address = pool
        self.entry_price_usd = 1.0
        self.max_price_usd = 2.5 if status == "win" else 1.2
        self.min_price_usd = 0.9
        self.max_multiple = 2.5 if status == "win" else 1.2
        self.max_drawdown_pct = 10.0
        self.screening_entry_usd = 1.0
        self.screening_target_usd = 2.0
        self.target_usd = 2.0
        self.target_2x_reached = status == "win"
        self.minus_50_reached = False
        self.time_2x_reached = None
        self.time_minus_50_reached = None
        self.which_threshold_first = "2x" if status == "win" else "none"
        self.api_requests_used = 1
        self.granular_analysis_required = False
        self.option2_entry = False
        self.evaluation_end_timestamp = end7


def _pair(status, complete, note, pool=POOL, end7=None):
    win = status == "win"
    br = BacktestResult(entry_price_usd=1.0,
                        peak_price_usd=2.5 if win else (1.2 if status == "loss" else None),
                        peak_timestamp=None, peak_profit_pct=150.0 if win else 20.0,
                        is_win=win, status=status, pool_address=pool, error=None)
    sl = StoplossResult(entry_price_usd=1.0,
                        peak_price_usd=2.5 if win else 1.2,
                        peak_timestamp=None, peak_profit_pct=150.0 if win else 20.0,
                        hit_stoploss=False, stoploss_timestamp=None,
                        is_win=win, status=status, pool_address=pool, error=None)
    return br, sl, FakeEval(status=status, complete=complete, note=note,
                            pool=pool, end7=end7)


@pytest.fixture()
def live_env(tmp_path, monkeypatch):
    """Fresh scratch unified DB + 7d engine + empty Telegram fetch +
    swappable price_one_call. Yields (db_path, pipeline, control, seed)."""
    import dataclasses
    import pipeline

    db_path = tmp_path / "live.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        (config.PROJECT_ROOT / "schema_unified.sql").read_text())
    conn.commit()
    conn.close()

    # Drop any thread-local connection opened against the old path.
    old = dbmod._local.__dict__.get("conn")
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
        dbmod._local.__dict__.pop("conn", None)

    new_settings = dataclasses.replace(
        config.settings, db_path=db_path, schema_file="schema_unified.sql",
        pricing_engine="7d", eval_days=7)
    monkeypatch.setattr(config, "settings", new_settings)
    monkeypatch.setattr(pipeline, "settings", new_settings)
    monkeypatch.setattr(dbmod, "settings", new_settings)
    import pricing.backtest as _bt
    monkeypatch.setattr(_bt, "settings", new_settings, raising=False)

    # run_backfill must not touch Telegram: one no-address message (so the
    # parser finds nothing) carrying the SAME telegram id as ensure_channel's
    # seed row — otherwise run_backfill invents a second channel via the
    # hash fallback and the seeded call is orphaned.
    from models import RawMessage

    def fake_fetch(ref, ws, we, limit=None, progress_cb=None):
        if progress_cb:
            progress_cb(1)
        return ([RawMessage(channel_id=555000, message_id=999,
                            text="no calls here", timestamp=ws)],
                "Live Tests")

    monkeypatch.setattr(pipeline, "_import_fetch_window_sync", lambda: fake_fetch)

    ctrl: dict = {"next": None}

    def fake_price_one_call(chain, client, addr, call_ts, now=None):
        spec = ctrl["next"]
        fn = spec.get(addr) or spec["*"]
        return fn(now=now, call_ts=call_ts)

    monkeypatch.setattr(pipeline, "price_one_call", fake_price_one_call)

    def seed(addr, call_ts):
        cid = pipeline.ensure_channel("@livetests", 555000, "Live Tests",
                                      "livetests",
                                      call_ts - timedelta(hours=1),
                                      datetime.utcnow())
        pc = ParsedCall(channel_id=cid, message_id=1, raw_text=f"$T {addr}",
                        token_address=addr, token_symbol="T",
                        token_name=None, timestamp=call_ts)
        pipeline.persist_parsed_call(cid, pc, 1, chain="sol")
        return cid

    return db_path, pipeline, ctrl, seed


def _q(db_path, sql):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql).fetchall()]
    finally:
        c.close()


def test_live_backfill_rescore_flip_finalize(live_env):
    db_path, pipeline, ctrl, seed = live_env
    now = datetime.utcnow().replace(microsecond=0)
    call_ts = now - timedelta(hours=3)
    seed(SOL, call_ts)

    # Pass 1 (young): provisional LOSS, counted by readers, tagged live.
    ctrl["next"] = {"*": lambda now, call_ts: _pair(
        "loss", complete=False, note="provisional loss (window open)",
        end7=call_ts + timedelta(days=7))}
    prog = pipeline.run_backfill("@livetests", call_ts - timedelta(hours=2),
                                 now, title="Live Tests", username="livetests")
    assert prog.priced == 1 and prog.live == 1
    row = _q(db_path, "SELECT status, score_state, pending_reason FROM calls")[0]
    assert row["status"] == "loss" and row["score_state"] == "live"
    assert row["pending_reason"] is None

    from analysis.windowed import channel_stats_window
    st = channel_stats_window(1, "all", "normal", chain=None)
    assert st["total_calls"] == 1 and st["wins"] == 0  # live counts immediately

    # Pass 2: rescore with extended window — pump appeared => flip to WIN.
    ctrl["next"] = {"*": lambda now, call_ts: _pair(
        "win", complete=False, note="2x", end7=call_ts + timedelta(days=7))}
    assert pipeline.rescore_live_calls() == 1
    row = _q(db_path, "SELECT status, score_state, max_multiple FROM calls")[0]
    assert row["status"] == "win" and row["score_state"] == "live"
    assert row["max_multiple"] == 2.5

    st = channel_stats_window(1, "all", "normal", chain=None)
    assert st["wins"] == 1  # leaderboard/win-rate follow the flip

    # Pass 3: window elapsed => FINAL, leaves the live set, idempotent.
    ctrl["next"] = {"*": lambda now, call_ts: _pair(
        "win", complete=True, note="2x", end7=call_ts + timedelta(days=7))}
    assert pipeline.rescore_live_calls() == 1
    row = _q(db_path, "SELECT status, score_state FROM calls")[0]
    assert row["status"] == "win" and row["score_state"] == "final"
    assert pipeline.rescore_live_calls() == 0  # nothing left live


def test_waiting_for_data_retry_then_finalize(live_env):
    db_path, pipeline, ctrl, seed = live_env
    now = datetime.utcnow().replace(microsecond=0)
    call_ts = now - timedelta(minutes=10)
    seed(WAIT_ADDR, call_ts)

    # Young + 'no pool' => NOT finalized unpriceable: stays pending, tagged.
    def waiting(now, call_ts):
        if now is None:  # window closed => spec's final unpriceable
            br = BacktestResult(entry_price_usd=None, peak_price_usd=None,
                                peak_timestamp=None, peak_profit_pct=None,
                                is_win=False, status="unpriceable_loss",
                                pool_address=None, error="no pool")
            sl = StoplossResult(entry_price_usd=None, peak_price_usd=None,
                                peak_timestamp=None, peak_profit_pct=None,
                                hit_stoploss=False, stoploss_timestamp=None,
                                is_win=False, status="unpriceable_loss",
                                pool_address=None, error="no pool")
            fe = FakeEval(status="unpriceable_loss", complete=True,
                          note="no pool", pool=None,
                          end7=call_ts + timedelta(days=7))
            return br, sl, fe
        return _pair("unpriceable_loss", complete=False, note="no pool",
                     pool=None, end7=call_ts + timedelta(days=7))

    ctrl["next"] = {"*": waiting}
    prog = pipeline.run_backfill("@livetests", call_ts - timedelta(hours=1),
                                 now, title="Live Tests", username="livetests")
    assert prog.waiting == 1 and prog.priced == 0
    row = _q(db_path, "SELECT status, score_state, pending_reason FROM calls")[0]
    assert row["status"] == "pending"
    assert row["pending_reason"] == "waiting_for_data"
    assert row["score_state"] == "live"

    # Still no data => stays in the retry lane.
    assert pipeline.rescore_live_calls() == 1
    row = _q(db_path, "SELECT status FROM calls")[0]
    assert row["status"] == "pending"

    # Simulate 8 days passing (window closed) => finalizes as unpriceable.
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE calls SET call_timestamp=? WHERE id=1",
                 ((now - timedelta(days=8)).isoformat() + "Z",))
    conn.commit()
    conn.close()
    assert pipeline.rescore_live_calls() == 1
    row = _q(db_path, "SELECT status, score_state, pending_reason FROM calls")[0]
    assert row["status"] == "unpriceable_loss"
    assert row["score_state"] == "final"
    assert row["pending_reason"] is None
    assert pipeline.rescore_live_calls() == 0


def test_backlog_immature_folds_into_live(live_env):
    db_path, pipeline, ctrl, seed = live_env
    now = datetime.utcnow().replace(microsecond=0)
    call_ts = now - timedelta(days=2)
    cid = seed(SOL, call_ts)
    # Emulate the pre-live-semantics deferred backlog rows:
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE calls SET score_state='final' WHERE id=1")
    conn.commit()
    conn.close()
    assert _q(db_path, "SELECT score_state FROM calls")[0]["score_state"] == "final"

    ctrl["next"] = {"*": lambda now, call_ts: _pair(
        "loss", complete=False, note="provisional loss (window open)",
        end7=call_ts + timedelta(days=7))}
    # immature_window pending rows get folded to live and priced.
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE calls SET status='pending', "
                 "pending_reason='immature_window' WHERE id=1")
    conn.commit()
    conn.close()
    assert pipeline.rescore_live_calls() == 1
    row = _q(db_path, "SELECT status, score_state, pending_reason FROM calls")[0]
    assert row["status"] == "loss" and row["score_state"] == "live"
