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


# ---- persistence --------------------------------------------------------------

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
