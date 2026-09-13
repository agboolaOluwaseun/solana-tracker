"""
cold_sim.py — Simulate the COLD-CACHE scenario: no token_meta, no price_cache.

ZERO DATABASE WRITES: every cache read/write is redirected to in-memory dicts,
so the real DB is untouched completely (not even normal caching writes).

Method: monkeypatch all cache touchpoints in memory, then price N recent
calls through price_calls_v2 (full pipeline: DexScreener resolution ladder
+ shared GeckoTerminal limiter).

Run: PYTHONPATH=. python3 scripts/cold_sim.py [N]
"""
import sys
import time
import logging

sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cold_sim")

import sqlite3
from datetime import datetime

# --- In-memory cache substitutes (DB stays 100% untouched) ------------------
import pricing.cache as cache_mod
import pricing.backtest as backtest_mod
import pricing.pipeline_v2 as pv2
from config import settings

# In-memory "token_meta": {address: row-dict}
mem_meta = {}
# In-memory "price_cache": {(pool, token): {ts: candle}}
mem_candles = {}

def _fake_get_cached_pool(addr):
    row = mem_meta.get(addr)
    if row and row.get("dex_pool_id"):
        from pricing.geckoterminal_v2 import _strip_network_prefix
        bare = _strip_network_prefix(row["dex_pool_id"], settings.solana_network)
        return {
            "pool_address": bare,
            "token_address": addr,
            "symbol": row.get("symbol"),
            "name": row.get("name"),
            "liquidity_usd": row.get("liquidity_usd"),
        }
    return None

def _fake_load_candles(pool, token, start, end, *a, **k):
    from datetime import timedelta
    store = mem_candles.get((pool, token), {})
    return [store[k] for k in sorted(store) if start <= k < end]

def _fake_store_candles(pool, token, candles, source="geckoterminal"):
    store = mem_candles.setdefault((pool, token), {})
    for c in candles:
        store[c.timestamp] = c

def _fake_upsert(pool_info):
    mem_meta[pool_info["token_address"]] = {
        "dex_pool_id": pool_info.get("pool_address"),
        "symbol": pool_info.get("symbol"),
        "name": pool_info.get("name"),
        "liquidity_usd": pool_info.get("liquidity_usd"),
    }

# Patch ALL touchpoints — pipeline Phase 1 AND backtest Phase 2
cache_mod.load_candles = _fake_load_candles
cache_mod.store_candles = _fake_store_candles
backtest_mod.get_cached_pool = _fake_get_cached_pool
pv2._get_cached_pool = _fake_get_cached_pool
pv2._upsert_token_meta = _fake_upsert
# backtest.upsert_token_meta writes Phase-2 fallback resolutions — redirect too
backtest_mod.upsert_token_meta = _fake_upsert
log.info("All cache I/O redirected to memory — DATABASE IS NOT TOUCHED")

from pricing.geckoterminal_v2 import GeckoTerminalClientV2
from pricing.pipeline_v2 import price_calls_v2

# --- Pick N recent calls (recent enough to avoid the 180-day 401 wall) ------
N = int(sys.argv[1]) if len(sys.argv) > 1 else 8
conn = sqlite3.connect("solana_tracker.db")
conn.row_factory = sqlite3.Row
calls = conn.execute("""
    SELECT ca.id, ca.token_address, ca.call_timestamp, ch.title
    FROM calls ca JOIN channels ch ON ch.id = ca.channel_id
    WHERE ca.call_timestamp > '2026-07-01'
    ORDER BY ca.call_timestamp DESC
    LIMIT ?
""", (N,)).fetchall()
print(f"\nCold simulation: {len(calls)} recent calls (all caches in-memory)\n")

shared_client = GeckoTerminalClientV2()
call_dicts = [{"token_address": r["token_address"], "call_timestamp": r["call_timestamp"]} for r in calls]

t_start = time.monotonic()
results = price_calls_v2(call_dicts, client=shared_client)
total = time.monotonic() - t_start

print("\n" + "=" * 70)
wins = sum(1 for _, bt, _ in results if bt.is_win)
losses = len(results) - wins
print(f"COLD RESULTS ({len(calls)} calls)")
print(f"  Total time:      {total:.1f}s ({total/60:.2f} min)")
print(f"  Per call:        {total/len(calls):.1f}s avg")
print(f"  GeckoTerminal requests: {shared_client.request_count} "
      f"= {shared_client.request_count/len(calls):.2f} per call")
print(f"  Throttles (429): {shared_client.throttle_count}")
if total > 0:
    print(f"  Effective GT rate: {shared_client.request_count/(total/60):.1f} req/min")
print(f"  Outcomes: {wins} wins, {losses} losses")
print()
print(f"EXTRAPOLATION to 142 calls (666 channel size):")
print(f"  Time:     {total/len(calls)*142/60:.1f} min")
print(f"  Requests: ~{int(shared_client.request_count/len(calls)*142)}")
