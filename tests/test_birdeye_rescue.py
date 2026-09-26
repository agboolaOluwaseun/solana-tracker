"""Birdeye cross-chain rescue tests — no network, scratch DB.

Proves: gating (no key / flag off), chain-first probing order, the
address-first find, scoring rescued candles through the REAL 7d engine,
chain_corrected persistence via apply_eval7d, and the circuit breaker.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import config
import pricing.birdeye_rescue as br

CALL_TS = datetime(2026, 6, 1, 12, 0, 0)
ADDR = "0x" + "ab" * 20


def _pp(ts, o, h, l, c):
    from models import PricePoint
    return PricePoint(timestamp=ts, open=o, high=h, low=l, close=c, volume=0.0)


class FakeResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def json(self):
        return self._p


def hourly_rows(prices):
    """1H candles starting one hour before CALL_TS."""
    t0 = int((CALL_TS - timedelta(hours=1)).replace(tzinfo=timezone.utc).timestamp())
    out = []
    base = 1.0
    for i, p in enumerate(prices):
        out.append({"unixTime": t0 + i * 3600, "o": base, "h": p, "l": base,
                    "c": p, "vol": 100})
        base = p
    return {"success": True, "data": {"items": out}}


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    br._chain_cache.clear()
    br.reset_breaker()
    import dataclasses
    # Settings is frozen: rebind a patched copy on the rescue module (it
    # captured `settings` at import time).
    monkeypatch.setattr(br, "settings", dataclasses.replace(
        config.settings, birdeye_api_key="k" * 32, birdeye_rescue=True),
        raising=False)
    # no pacing sleeps in tests
    monkeypatch.setattr(br, "_pace", lambda kind, interval: None)
    yield
    br._chain_cache.clear()


def _flag_off(monkeypatch, key=True, rescue=False):
    """Patch the gating flags for one test (fresh frozen copy each time)."""
    import dataclasses
    monkeypatch.setattr(br, "settings", dataclasses.replace(
        config.settings,
        birdeye_api_key="k" * 32 if key else "",
        birdeye_rescue=rescue), raising=False)


def make_get(price_hits, ohlcv_payload=None):
    """Fake _get: /defi/price answers 200 w/ value iff chain in price_hits;
    /defi/ohlcv returns the given payload."""
    def fake_get(path, chain, params, kind, tries=3):
        if path == "/defi/price":
            if chain in price_hits:
                return FakeResp({"data": {"value": 0.5}})
            return FakeResp({"data": None})
        return FakeResp(ohlcv_payload or hourly_rows([1.0] * 168))
    return fake_get


# ---- gating ----------------------------------------------------------------

def test_disabled_without_key(monkeypatch):
    _flag_off(monkeypatch, key=False, rescue=True)
    monkeypatch.setattr(br, "_get", make_get({"base"}))
    assert br.try_rescue(ADDR, "robinhood", CALL_TS) is None


def test_disabled_by_flag(monkeypatch):
    _flag_off(monkeypatch, key=True, rescue=False)
    monkeypatch.setattr(br, "_get", make_get({"base"}))
    assert br.try_rescue(ADDR, "robinhood", CALL_TS) is None


def test_no_chain_anywhere_returns_none(monkeypatch):
    monkeypatch.setattr(br, "_get", make_get(set()))
    assert br.try_rescue(ADDR, "robinhood", CALL_TS) is None


# ---- address-first probing ---------------------------------------------------

def test_find_chain_probes_tagged_first(monkeypatch):
    order = []
    def fake_get(path, chain, params, kind, tries=3):
        if path == "/defi/price":
            order.append(chain)
            return FakeResp({"data": {"value": 1.0} if chain == "ethereum" else None})
        return FakeResp({})
    monkeypatch.setattr(br, "_get", fake_get)
    # tagged 'eth' -> ethereum is probed FIRST and hit short-circuits
    assert br.find_chain(ADDR, "eth") == "ethereum"
    assert order == ["ethereum"]


def test_find_chain_crosses_when_wrongly_tagged(monkeypatch):
    """A token tagged robinhood that only exists on base must still be found."""
    order = []
    def fake_get(path, chain, params, kind, tries=3):
        order.append(chain)
        return FakeResp({"data": {"value": 1.0} if chain == "base" else None})
    monkeypatch.setattr(br, "_get", fake_get)
    assert br.find_chain(ADDR, "robinhood") == "base"
    # robinhood probed first (wrong tag), then bsc/base...  base within EVM set
    assert order[0] == "robinhood" and "base" in order


def test_base58_only_probes_solana(monkeypatch):
    order = []
    def fake_get(path, chain, params, kind, tries=3):
        order.append(chain)
        return FakeResp({"data": None})
    monkeypatch.setattr(br, "_get", fake_get)
    br.find_chain("So11111111111111111111111111111111111111112", "sol")
    assert order == ["solana"]  # never probed EVM chains


# ---- scoring through the real engine ------------------------------------------

def test_rescue_win_and_chain_correction(monkeypatch):
    monkeypatch.setattr(br, "_get", make_get({"bsc"}, hourly_rows(
        [1.0] * 10 + [2.5] + [1.0] * 158)))
    monkeypatch.setattr(br, "_identity", lambda a, c: (None, "RESC", "Rescued"))
    r = br.try_rescue(ADDR, "robinhood", CALL_TS)
    assert r is not None
    assert r.status_plain == "win"
    assert r.chain_corrected == "bsc"
    assert r.token_symbol == "RESC"
    assert "birdeye" in r.note


def test_rescue_respects_live_mode(monkeypatch):
    """With now= inside the window the rescued verdict is provisional
    (window_complete False), never a frozen final."""
    monkeypatch.setattr(br, "_get", make_get({"base"}, hourly_rows([1.0] * 169)))
    monkeypatch.setattr(br, "_identity", lambda a, c: (None, None, None))
    r = br.try_rescue(ADDR, "eth", CALL_TS, now=CALL_TS + timedelta(days=2))
    assert r is not None and r.window_complete is False
    assert r.chain_corrected == "base"


def test_rescue_no_candles_keeps_verdict(monkeypatch):
    monkeypatch.setattr(br, "_get", make_get({"bsc"}, {"success": True, "data": {"items": []}}))
    assert br.try_rescue(ADDR, "robinhood", CALL_TS) is None


def test_net_failure_never_raises(monkeypatch):
    def dead(path, chain, params, kind, tries=3):
        return None
    monkeypatch.setattr(br, "_get", dead)
    assert br.try_rescue(ADDR, "sol", CALL_TS) is None


def test_breaker_disables_after_repeated_failures(monkeypatch):
    """Trip the breaker through the REAL _get error path (session raising),
    so the counting logic itself is under test."""
    import requests as _rq

    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise _rq.ConnectionError("simulated outage")
    monkeypatch.setattr(br._session, "get", boom)
    for _ in range(8):
        assert br.try_rescue(ADDR, "sol", CALL_TS) is None
    assert not br.enabled()          # breaker tripped
    n_after = calls["n"]
    br.try_rescue(ADDR, "sol", CALL_TS)
    assert calls["n"] == n_after     # no further requests made
    br.reset_breaker()
    assert br.enabled()


def test_big_hourly_multiple_requires_minute_confirmation(monkeypatch):
    """A rescued verdict whose hourly peak >= 50x is UNTRUSTED by itself
    (CALI's 12,590x was one minute inside a 1H bar). try_rescue must demand
    the wick-clipped 1m series; if that's unobtainable the suspect verdict
    is refused, never persisted."""
    monkeypatch.setattr(br, "find_chain", lambda a, c: "solana")
    spike = hourly_rows([1.0] * 10 + [77.0] + [1.0] * 158)   # one 1H bar 77x
    monkeypatch.setattr(br, "_get", lambda path, chain, params, kind, tries=3:
                        FakeResp(spike if path == "/defi/ohlcv" else {"data": {"value": 1}}))
    # no 1m available: _confirm_on_minutes sees <240 points -> None
    monkeypatch.setattr(br, "_identity", lambda a, c: (None, "BIG", None))
    r = br.try_rescue(ADDR, "sol", CALL_TS)
    assert r is None, "suspect hourly-only verdict must be refused"


def test_big_multiple_survives_when_minutes_agree(monkeypatch):
    """Same 77x, but the 1m series shows the peak SUSTAINED across many
    minutes (real pump, not a wick) -> confirmation passes and the win
    persists."""
    monkeypatch.setattr(br, "find_chain", lambda a, c: "solana")
    spike = hourly_rows([1.0] * 10 + [77.0] + [1.0] * 158)
    # 1m series: the 77x hour filled with ~50 real minutes at rising prices
    t0 = int((CALL_TS - timedelta(hours=1)).replace(tzinfo=timezone.utc).timestamp()) // 3600 * 3600
    m_items = [{"unixTime": t0 + 60 * k, "o": 1.0, "h": 70.0 if 60 <= k < 120 else 1.0,
                "l": 1.0, "c": 70.0 if 60 <= k < 120 else 1.0, "vol": 1e3}
               for k in range(1000)]
    def fake_get(path, chain, params, kind, tries=3):
        if path != "/defi/ohlcv":
            return FakeResp({"data": {"value": 1}})
        if params.get("type") == "1m":
            return FakeResp({"success": True, "data": {"items": m_items}})
        return FakeResp(spike)
    monkeypatch.setattr(br, "_get", fake_get)
    monkeypatch.setattr(br, "_identity", lambda a, c: (None, "BIG", None))
    r = br.try_rescue(ADDR, "sol", CALL_TS)
    assert r is not None and r.status_plain == "win"
    assert "(1m wick-confirmed)" in r.note


# ---- pipeline gating: refresh must NEVER reach Birdeye ----------------------

def test_price_one_call_rescue_is_opt_in(monkeypatch):
    """allow_birdeye defaults False — only the initial-fetch call sites pass
    True. The refresh stream / rescore / reprice inherit the default, so a
    future refactor that forgets to pass the flag errs on NEVER calling
    Birdeye (user policy 2026-09-22)."""
    import pipeline
    from pricing import backtest as bt

    seen = []
    monkeypatch.setattr(br, "try_rescue",
                        lambda *a, **k: seen.append(k) or None)
    # unresolvable everywhere -> engine lands on unpriceable_loss ('no pool')
    monkeypatch.setattr(bt, "get_cached_pool",
                        lambda addr, chain="sol": None)

    class _NullDS:
        def resolve_pool(self, addr, chain="solana"):
            return None
    monkeypatch.setattr(pipeline, "_get_ds_client", _NullDS)
    monkeypatch.setattr(pipeline, "_get_v2_client", lambda chain: object())

    br.reset_breaker()
    _, _, r7 = pipeline.price_one_call("eth", None, ADDR, CALL_TS)
    assert r7.status_plain == "unpriceable_loss"
    assert seen == []                      # default: rescue NOT attempted

    pipeline.price_one_call("eth", None, ADDR, CALL_TS, allow_birdeye=True)
    assert len(seen) == 1                  # fetch path: rescue attempted


def test_run_backfill_default_closed():
    import inspect
    import pipeline
    sig = inspect.signature(pipeline.run_backfill)
    assert sig.parameters["birdeye_rescue"].default is False
    sig2 = inspect.signature(pipeline.price_one_call)
    assert sig2.parameters["allow_birdeye"].default is False


def test_refresh_stream_source_closed():
    """Static guard: api/server.py's _one_channel (refresh) must pass
    birdeye_rescue=False explicitly; fetch sites pass True."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "api" / "server.py"
    text = src.read_text()
    refresh = text.split("async def _one_channel")[1].split("async def")[0]
    fetch1 = text.split("async def _fetch_stream_inner")[1].split("async def")[0]
    assert "birdeye_rescue=False" in refresh
    assert "birdeye_rescue=True" in fetch1
    assert "birdeye_rescue" not in refresh.replace("birdeye_rescue=False", "")

def test_apply_eval7d_adopts_rescued_chain(tmp_path, monkeypatch):
    """A rescued result's chain_corrected overwrites the stored tag; a
    normal result (None) leaves it untouched."""
    import dataclasses
    import db as dbmod
    import pipeline
    from pipeline import apply_eval7d
    from pricing.strategy7d import Eval7dResult

    dbfile = tmp_path / "resc.db"
    new_settings = dataclasses.replace(
        config.settings, db_path=dbfile, schema_file="schema_unified.sql")
    monkeypatch.setattr(config, "settings", new_settings)
    monkeypatch.setattr(pipeline, "settings", new_settings)
    monkeypatch.setattr(dbmod, "settings", new_settings)
    # drop stale thread-local conn bound to the real DB
    old = dbmod._local.__dict__.get("conn")
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
        dbmod._local.__dict__.pop("conn", None)

    dbmod.init_db()
    with dbmod.transaction() as conn:
        conn.execute(
            """INSERT INTO channels (telegram_channel_id, title,
                 window_start, window_end) VALUES (1,'x','2026-01-01','2026-09-01')""")
        ch_id = conn.execute("SELECT id FROM channels").fetchone()["id"]
        conn.execute(
            """INSERT INTO calls (channel_id, message_id, raw_text,
                 token_address, call_timestamp, chain,
                 status, is_win)
               VALUES (?,1,'text',?,'2026-06-01T12:00:00Z',
                 'robinhood','unpriceable_loss',0)""",
            (ch_id, ADDR))
        call_id = conn.execute("SELECT id FROM calls").fetchone()["id"]

    base_r = Eval7dResult(status_plain="loss", status_stoploss="loss",
                          window_complete=True)
    apply_eval7d(call_id, base_r)
    with dbmod.get_connection() as c:
        assert c.execute("SELECT chain FROM calls WHERE id=?", (call_id,)).fetchone()["chain"] == "robinhood"

    rescued = Eval7dResult(status_plain="win", status_stoploss="win",
                           window_complete=True, chain_corrected="bsc",
                           token_symbol="RESC")
    apply_eval7d(call_id, rescued)
    with dbmod.get_connection() as c:
        row = c.execute("SELECT chain, token_symbol FROM calls WHERE id=?", (call_id,)).fetchone()
    assert row["chain"] == "bsc"
    assert row["token_symbol"] == "RESC"


def test_apply_eval7d_persists_embedded_trailing_verdict(tmp_path, monkeypatch):
    """Rescued calls have no GT-cached curve, so the rescue embeds its
    trail verdict (computed on the SAME Birdeye series as normal/SL) and
    apply_eval7d must write it to trailing_results."""
    import dataclasses
    import db as dbmod
    import pipeline
    from pipeline import apply_eval7d, persist_trailing_result
    from pricing.strategy7d import Eval7dResult
    from pricing.trailing import TrailingResult
    from datetime import datetime

    dbfile = tmp_path / "emb.db"
    new_settings = dataclasses.replace(
        config.settings, db_path=dbfile, schema_file="schema_unified.sql")
    monkeypatch.setattr(config, "settings", new_settings)
    monkeypatch.setattr(pipeline, "settings", new_settings)
    monkeypatch.setattr(dbmod, "settings", new_settings)
    old = dbmod._local.__dict__.get("conn")
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
        dbmod._local.__dict__.pop("conn", None)
    dbmod.init_db()
    with dbmod.transaction() as conn:
        conn.execute("INSERT INTO channels (telegram_channel_id, title,"
                     " window_start, window_end) VALUES (1,'x','2026-01-01',"
                     "'2026-09-01')")
        ch = conn.execute("SELECT id FROM channels").fetchone()["id"]
        conn.execute("INSERT INTO calls (channel_id, message_id, raw_text,"
                     " token_address, call_timestamp, chain, status, is_win)"
                     " VALUES (?,1,'t','0xabc','2026-06-01T12:00:00Z',"
                     "'robinhood','unpriceable_loss',0)", (ch,))
        cid = conn.execute("SELECT id FROM calls").fetchone()["id"]

    tv = TrailingResult(
        entry_price_usd=1e-4, peak_multiple=1.8, peak_price_usd=1.8e-4,
        peak_timestamp=datetime(2026, 6, 2), exit_multiple=0.9,
        exit_price_usd=9e-5, exit_timestamp=datetime(2026, 6, 3),
        loss_pct=-10.0, hit_trailing_stop=True, is_win=False,
        status="loss", pool_address="0xabc")
    r = Eval7dResult(status_plain="win", status_stoploss="win",
                     entry_price_usd=1e-4, max_multiple=2.5,
                     window_complete=True, trailing_verdict=tv)
    # persist_stoploss's cache-attach finds NO candles (rescued pool) and
    # must not clobber the embedded verdict: attach returns None -> skipped.
    pipeline.persist_stoploss_result(cid, r and __import__("pipeline").eval7d_to_legacy_pair(r)[1])
    apply_eval7d(cid, r)
    with dbmod.get_connection() as c:
        row = c.execute("SELECT status, exit_multiple, loss_pct,"
                        " entry_price_usd FROM trailing_results"
                        " WHERE call_id=?", (cid,)).fetchone()
    assert row is not None and row["status"] == "loss"
    assert row["exit_multiple"] == 0.9 and row["loss_pct"] == -10.0
    assert row["entry_price_usd"] == 1e-4      # rescue's own entry dollar
    # cleanup thread-local so next tests rebind cleanly
    emb = dbmod._local.__dict__.get("conn")
    if emb is not None:
        emb.close()
        dbmod._local.__dict__.pop("conn", None)
