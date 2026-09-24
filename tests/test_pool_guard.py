"""Pool-orientation guard (museic/DRUGS lesson, 2026-09-23).

Two layers:
  * pure tripwire/parse tests (pricing/pool_guard.py)
  * dispatcher integration: the REAL pipeline.price_one_call with fake
    fetchers/meta — proves flipped pools are refused or re-seated and that
    healthy calls pay ZERO extra API calls.
"""
import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

import config
import pricing.backtest as bt
import pricing.pool_guard as pg

T0 = datetime(2026, 9, 22, 15, 0)          # naive UTC — engine convention

MUSEIC = "0xed97b68ae1be330963eb161a11beb1cf5fe71ba3"
META = "0xc0d6457c16cc70d6790dd43521c899c87ce02f35"
FLIP_POOL = "0xb5600a49c1a3fe21e8a2fb97e6ca0241d3fb0999915b9443f1c9a9986d75abcc"
BASE_POOL = "0xc59d4307c379eee9a211dea34f48964ae87777f86e8460a50190f6f6cc15886a"

# GT /tokens/{addr}/pools listing shape (trimmed to guard-relevant fields)
MUSEIC_LISTING = {
    "data": [
        {"id": "robinhood_" + FLIP_POOL,
         "attributes": {"address": FLIP_POOL, "name": "META / museic",
                        "reserve_in_usd": "125609.51"},
         "relationships": {
             "base_token": {"data": {"id": "robinhood_" + META}},
             "quote_token": {"data": {"id": "robinhood_" + MUSEIC}}}},
        {"id": "robinhood_" + BASE_POOL,
         "attributes": {"address": BASE_POOL, "name": "museic / USDG 5%",
                        "reserve_in_usd": "12313.13"},
         "relationships": {
             "base_token": {"data": {"id": "robinhood_" + MUSEIC}},
             "quote_token": {"data": {"id": "robinhood_0xusdg"}}}},
        {"id": "robinhood_0xjunk",
         "attributes": {"address": "0xjunk", "name": "museic / WETH 50%",
                        "reserve_in_usd": "7.46"},
         "relationships": {
             "base_token": {"data": {"id": "robinhood_" + MUSEIC}},
             "quote_token": {"data": {"id": "robinhood_0xweth"}}}},
    ]
}


# ── pure tripwires ──────────────────────────────────────────────────────────

def test_exotic_quote_flags_stock_token_pair():
    assert pg.is_exotic_quote("robinhood", "META")
    assert pg.is_exotic_quote("solana", "BIO")


def test_safe_quotes_are_not_flagged():
    assert not pg.is_exotic_quote("solana", "SOL")
    assert not pg.is_exotic_quote("solana", "USDC")
    assert not pg.is_exotic_quote("robinhood", "USDC")
    assert not pg.is_exotic_quote("eth", "WETH")
    assert not pg.is_exotic_quote("arc", "USDC")
    assert not pg.is_exotic_quote("solana", None)      # no signal -> no flag


def test_magnitude_needs_20x_divergence():
    assert pg.magnitude_suspect(733.0, 1.96e-6, 1.0)      # museic: 3.7e8×
    assert pg.magnitude_suspect(0.0273, 0.000314, 1.0)    # DRUGS: 87×
    assert not pg.magnitude_suspect(0.000653, 0.000652, 1.0)   # healthy ~1.0×
    assert not pg.magnitude_suspect(0.005, 0.001, 1.0)     # 5× — legit drift
    assert not pg.magnitude_suspect(0.01, 0.001, 1.0)      # 10× — under 20× gate


def test_magnitude_exempt_for_stale_reference():
    # months-old backfill window: curve-vs-live-quote divergence is real drift
    assert not pg.magnitude_suspect(0.002, 0.00001, ref_age_hours=720)
    assert pg.magnitude_suspect(0.002, 0.00001, ref_age_hours=6)


def test_parse_pools_reads_seats_and_quality_gates():
    is_base, cands = pg.parse_pools(MUSEIC_LISTING, MUSEIC, FLIP_POOL)
    assert is_base is False                       # META / museic: flipped
    assert [c["pool_address"] for c in cands] == [BASE_POOL]   # $12k only;
    # the $7 WETH pool fails the $1000 reserve gate — junk never scored
    is_base2, _ = pg.parse_pools(MUSEIC_LISTING, MUSEIC, BASE_POOL)
    assert is_base2 is True
    is_base3, _ = pg.parse_pools({}, MUSEIC, FLIP_POOL)
    assert is_base3 is None                       # pool not in listing


# ── dispatcher integration (REAL price_one_call) ───────────────────────────

@pytest.fixture()
def dispatch_env(monkeypatch):
    """Scratch settings + fake pool meta/fetchers/clients around the real
    pipeline.price_one_call. Returns control dict + recorded calls."""
    import pipeline

    new_settings = dataclasses.replace(config.settings,
                                       pricing_engine="7d", eval_days=7,
                                       pool_guard=True, birdeye_rescue=True,
                                       geckoterminal_api_key=None)
    monkeypatch.setattr(config, "settings", new_settings)
    monkeypatch.setattr(pipeline, "settings", new_settings)

    ctrl = {"pool_info": None, "confirm": None, "rescued": None,
            "scored_pools": [], "ups": [], "listing_calls": 0}

    monkeypatch.setattr(bt, "get_cached_pool",
                        lambda addr, chain="sol": ctrl["pool_info"])
    # price_one_call imports these from pricing.backtest INSIDE the function
    # body — patch the SOURCE module, not pipeline, or the real upsert runs
    # against the production DB.
    monkeypatch.setattr(bt, "upsert_token_meta",
                        lambda meta, chain="sol": ctrl["ups"].append(dict(meta)))
    monkeypatch.setattr(pipeline, "_get_ds_client", lambda: None)
    monkeypatch.setattr(pipeline, "_get_v2_client", lambda chain: "dummy-client")

    def fake_confirm(client, addr, pool):
        ctrl["listing_calls"] += 1
        return ctrl["confirm"] if ctrl["confirm"] else (None, [])
    monkeypatch.setattr(pg, "confirm_seats", fake_confirm)

    # engine sees constant price $1 flat candles; records which pool it scored
    def fake_fetchers(conn, client):
        def fh(pool, token, start, end):
            ctrl["scored_pools"].append(pool)
            from models import PricePoint
            out, t = [], start
            while t <= end:
                out.append(PricePoint(timestamp=t, open=1.0, high=1.0,
                                      low=1.0, close=1.0, volume=1))
                t += timedelta(hours=1)
            return out, 0
        def fm(pool, token, start, end):
            from models import PricePoint
            t = start.replace(second=0, microsecond=0)
            out = []
            while t <= end:
                out.append(PricePoint(timestamp=t, open=1.0, high=1.0,
                                      low=1.0, close=1.0, volume=1))
                t += timedelta(minutes=1)
            return out, 0
        return fh, fm
    monkeypatch.setattr("pricing.strategy7d_cache.make_cached_fetchers",
                        fake_fetchers)

    def fake_rescue(addr, chain, ts, now=None):
        return ctrl["rescued"]
    monkeypatch.setattr("pricing.birdeye_rescue.try_rescue", fake_rescue)
    return pipeline, ctrl


def _pool(**over):
    base = {"token_address": MUSEIC, "pool_address": FLIP_POOL,
            "symbol": "museic", "name": "museic", "liquidity_usd": 113000,
            "quote_symbol": "META", "price_usd": "0.00000196"}
    base.update(over)
    return base


def test_cached_flip_refused_and_never_scored(dispatch_env):
    pipeline, ctrl = dispatch_env
    ctrl["pool_info"] = _pool(pool_is_base=0)
    result, sl, r7 = pipeline.price_one_call(
        "robinhood", None, MUSEIC, T0, now=T0 + timedelta(hours=2))
    assert r7.status_plain == "unpriceable_loss"
    assert r7.note == "orientation ambiguous"
    assert not r7.window_complete          # -> score_state 'waiting' -> retried
    assert ctrl["scored_pools"] == []      # the wrong curve was NOT scored
    assert ctrl["listing_calls"] == 0      # cached verdict: no re-listing


def test_exotic_pool_reseated_to_base_pool(dispatch_env):
    pipeline, ctrl = dispatch_env
    ctrl["pool_info"] = _pool()                       # seat unknown
    ctrl["confirm"] = (False, [{"pool_address": BASE_POOL,
                                "reserve_usd": 12313.0,
                                "name": "museic / USDG 5%"}])
    result, sl, r7 = pipeline.price_one_call(
        "robinhood", None, MUSEIC, T0, now=T0 + timedelta(hours=2))
    assert ctrl["listing_calls"] == 1
    assert ctrl["scored_pools"] and ctrl["scored_pools"][0] == BASE_POOL
    assert r7.status_plain in ("win", "loss")          # scored on right curve
    assert any(u.get("pool_is_base") == 1 for u in ctrl["ups"])


def test_flip_without_gt_pool_goes_to_birdeye_on_fetch(dispatch_env):
    from pricing.strategy7d import Eval7dResult
    pipeline, ctrl = dispatch_env
    ctrl["pool_info"] = _pool()
    ctrl["confirm"] = (False, [])                      # no usable candidate
    rescued = Eval7dResult(status_plain="win", status_stoploss="win",
                           entry_price_usd=2.6e-6, max_multiple=2.9,
                           note="2x (birdeye robinhood)")
    ctrl["rescued"] = rescued
    result, sl, r7 = pipeline.price_one_call(
        "robinhood", None, MUSEIC, T0, now=T0 + timedelta(hours=2),
        allow_birdeye=True)
    assert r7 is rescued                               # (b) branch used
    assert ctrl["scored_pools"] == []                  # never GT-scored flipped


def test_refused_without_birdeye_stays_waiting(dispatch_env):
    pipeline, ctrl = dispatch_env
    ctrl["pool_info"] = _pool()
    ctrl["confirm"] = (False, [])
    result, sl, r7 = pipeline.price_one_call(
        "robinhood", None, MUSEIC, T0, now=T0 + timedelta(hours=2),
        allow_birdeye=False)                           # refresh path
    assert r7.note == "orientation ambiguous"
    assert pipeline.score_state_7d(r7) == "waiting"    # retried next pass


def test_magnitude_tripwire_catches_unlisted_flip(dispatch_env):
    """Seat listing unavailable (failed) + safe-looking quote: the curve/quote
    magnitude gap still refuses — but only while the window is fresh."""
    pipeline, ctrl = dispatch_env
    ctrl["pool_info"] = _pool(quote_symbol="USDC")     # quote looks safe
    ctrl["confirm"] = (None, [])                       # listing failed
    # fake fetchers must serve a $1 curve while DS says $1.96e-6 -> 5e5× gap.
    # Override price_usd so divergence is huge but the pool looked clean:
    ctrl["pool_info"]["price_usd"] = "0.00000196"
    result, sl, r7 = pipeline.price_one_call(
        "robinhood", None, MUSEIC, T0, now=T0 + timedelta(hours=2))
    assert r7.note == "orientation ambiguous"          # magnitude flagged it
    assert r7.status_plain == "unpriceable_loss"


def test_final_window_exempt_from_magnitude(dispatch_env):
    """Months-old (complete) window: live quote vs window-end close is real
    drift — must NOT refuse; exotic-quote tripwire still applies though."""
    pipeline, ctrl = dispatch_env
    ctrl["pool_info"] = _pool(quote_symbol="USDC")
    ctrl["confirm"] = (None, [])
    # window already complete: now=None and call long ago -> engine final path
    old = datetime(2026, 5, 1, 12, 0)
    result, sl, r7 = pipeline.price_one_call(
        "robinhood", None, MUSEIC, old)                # no `now` -> full window
    assert r7.window_complete
    assert r7.note != "orientation ambiguous"          # old curve scored as before


def test_healthy_usdc_call_pays_zero_extra_calls(dispatch_env):
    pipeline, ctrl = dispatch_env
    ctrl["pool_info"] = _pool(quote_symbol="USDC",
                              price_usd="1.0")         # curve=$1 -> ratio 1.0
    result, sl, r7 = pipeline.price_one_call(
        "robinhood", None, MUSEIC, T0, now=T0 + timedelta(hours=2))
    assert r7.status_plain in ("win", "loss")
    assert ctrl["listing_calls"] == 0                  # no tripwire fired
    assert ctrl["ups"] == []                           # nothing even cached


def test_guard_disabled_via_config(dispatch_env, monkeypatch):
    import pipeline
    monkeypatch.setattr(pipeline, "settings",
                        dataclasses.replace(pipeline.settings, pool_guard=False))
    pipeline, ctrl = dispatch_env
    ctrl["pool_info"] = _pool(pool_is_base=0)          # known flip
    result, sl, r7 = pipeline.price_one_call(
        "robinhood", None, MUSEIC, T0, now=T0 + timedelta(hours=2))
    # POOL_GUARD=false => old behavior restored (scores the curve regardless)
    assert r7.note != "orientation ambiguous"
    assert ctrl["scored_pools"]
