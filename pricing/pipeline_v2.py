"""
pipeline_v2.py — Optimized pricing pipeline (GeckoTerminal only, no Birdeye).

Optimizations:
  Layer 1: RateLimiterV2 + GeckoTerminalClientV2 (better params, no double jitter)
  Layer 2: Cache-first pool resolution (check token_meta BEFORE hitting API)
  Layer 3: Single shared client for both phases (one limiter instance)

Key insight: most pools are already in token_meta from previous runs.
Phase 1 only resolves UNKNOWN tokens via GeckoTerminal (sequential, rate-limited).

Usage:
    from pricing.pipeline_v2 import price_calls_v2

    results = price_calls_v2(calls)  # list of (call_id, BacktestResult, StoplossResult)

This file does NOT touch the main pipeline.py — it's a standalone module
that can be tested independently before integration.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import List, Optional, Tuple

from config import settings
from db import get_connection, transaction
from models import BacktestResult, StoplossResult
from pricing.geckoterminal_v2 import GeckoTerminalClientV2, GeckoTerminalError

log = logging.getLogger(__name__)


# ---- Cache-First Pool Resolution ------------------------------------------

def _get_cached_pool(token_address: str) -> Optional[dict]:
    """Check token_meta cache for a pool. Returns pool info or None."""
    try:
        conn = get_connection()
        conn.row_factory = __import__("sqlite3").Row
        row = conn.execute(
            "SELECT dex_pool_id, symbol, name, liquidity_usd FROM token_meta WHERE address = ?",
            (token_address,),
        ).fetchone()
        if row and row["dex_pool_id"]:
            from pricing.geckoterminal_v2 import _strip_network_prefix
            bare = _strip_network_prefix(row["dex_pool_id"], settings.solana_network)
            return {
                "pool_address": bare,
                "token_address": token_address,
                "symbol": row["symbol"],
                "name": row["name"],
                "liquidity_usd": row["liquidity_usd"],
            }
    except Exception as e:
        log.debug("Cache lookup failed for %s: %s", token_address[:10], e)
    return None


def resolve_pool_batch_sequential(
    token_addresses: List[str],
    client: GeckoTerminalClientV2,
) -> dict:
    """
    Resolve pools for a batch of token addresses.

    Resolution ladder (cheapest source first):
      1. token_meta cache (instant, 0 requests)
      2. DexScreener (free, no key, ~0.5s each — verified 6/6 identical pool
         selection to GeckoTerminal on real tracked tokens)
      3. GeckoTerminal search (rate-limited, last resort)

    Storing DexScreener results in token_meta means Phase 2's backtest_call
    finds them via get_cached_pool and makes ZERO resolution requests —
    the entire GeckoTerminal budget goes to OHLCV candles.

    Returns {token_address: pool_info_dict} for successfully resolved tokens.
    """
    from pricing.dexscreener import DexScreenerClient

    results = {}
    ds_client = DexScreenerClient()
    ds_misses = []

    # 1. Cache lookup (instant)
    for addr in token_addresses:
        cached = _get_cached_pool(addr)
        if cached:
            results[addr] = cached
        else:
            ds_misses.append(addr)

    log.info(
        "Pool resolution: %d/%d cached, %d to resolve",
        len(results), len(token_addresses), len(ds_misses),
    )

    # 2. DexScreener (free) for the rest
    gt_fallback = []
    for addr in ds_misses:
        try:
            resolved = ds_client.resolve_pool(addr)
        except Exception as e:
            log.debug("DexScreener error for %s: %s", addr[:10], e)
            resolved = None
        if resolved:
            results[addr] = resolved
            _upsert_token_meta(resolved)
        else:
            gt_fallback.append(addr)

    if ds_misses:
        log.info(
            "DexScreener resolved %d/%d, %d need GeckoTerminal fallback",
            len(ds_misses) - len(gt_fallback), len(ds_misses), len(gt_fallback),
        )

    # 3. GeckoTerminal fallback (rate-limited, uses the SHARED client limiter)
    for addr in gt_fallback:
        try:
            resolved = client.resolve_pool_smart(addr)
            if resolved:
                results[addr] = resolved
                _upsert_token_meta(resolved)
        except Exception as e:
            log.warning("GeckoTerminal pool resolution failed for %s: %s", addr[:10], e)

    log.info(
        "Pool resolution batch: %d/%d resolved", len(results), len(token_addresses)
    )
    return results


def _upsert_token_meta(pool_info: dict) -> None:
    """Store pool info to token_meta."""
    try:
        with transaction() as conn:
            conn.execute(
                """
                INSERT INTO token_meta (address, symbol, name, dex_pool_id, liquidity_usd)
                VALUES (:address, :symbol, :name, :dex_pool_id, :liquidity_usd)
                ON CONFLICT(address) DO UPDATE SET
                    symbol=excluded.symbol, name=excluded.name,
                    dex_pool_id=excluded.dex_pool_id, liquidity_usd=excluded.liquidity_usd
                """,
                {
                    "address": pool_info["token_address"],
                    "symbol": pool_info.get("symbol"),
                    "name": pool_info.get("name"),
                    "dex_pool_id": pool_info.get("pool_address"),
                    "liquidity_usd": pool_info.get("liquidity_usd"),
                },
            )
    except Exception as e:
        log.warning("Failed to upsert token_meta for %s: %s", pool_info.get("token_address", "?")[:10], e)


# ---- Main Pipeline --------------------------------------------------------

def price_calls_v2(
    calls: list,
    progress_callback=None,
    client: Optional[GeckoTerminalClientV2] = None,
) -> List[Tuple[int, BacktestResult, StoplossResult]]:
    """
    Price a list of calls using the optimized pipeline.

    Args:
        calls: list of objects with .token_address and .call_timestamp attributes,
               or list of dicts with 'token_address' and 'call_timestamp' keys.
        progress_callback: optional fn(completed, total) called after each call.
        client: optional shared GeckoTerminalClientV2 (for instrumentation/tests).

    Returns:
        list of (call_id_or_index, BacktestResult, StoplossResult) tuples.
    """
    from pricing.backtest import backtest_call

    # Normalize calls to dicts, parsing timestamps
    def _parse_ts(ts_val):
        """Parse a timestamp from string (SQLite) or datetime to naive UTC datetime."""
        if isinstance(ts_val, str):
            ts_str = ts_val.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
            return dt
        return ts_val

    call_list = []
    for i, c in enumerate(calls):
        if isinstance(c, dict):
            call_list.append((c.get("id", i), c["token_address"], _parse_ts(c["call_timestamp"])))
        else:
            call_list.append((getattr(c, "id", i), c.token_address, _parse_ts(c.call_timestamp)))

    total = len(call_list)

    # Single shared client for both phases (one limiter instance);
    # callers may inject one for instrumentation/testing.
    if client is None:
        client = GeckoTerminalClientV2()

    # Phase 1: Cache-first pool resolution
    unique_tokens = list({addr for _, addr, _ in call_list})
    log.info("Phase 1: Resolving %d unique pools (cache-first)...", len(unique_tokens))
    t0 = time.monotonic()
    pools = resolve_pool_batch_sequential(unique_tokens, client)
    phase1_time = time.monotonic() - t0
    log.info("Phase 1 complete: %d pools resolved in %.1fs", len(pools), phase1_time)

    # Phase 2: Sequential OHLCV fetch (rate-limited via V2 client)
    log.info("Phase 2: Fetching OHLCV for %d calls (rate-limited)...", total)
    results = []
    t1 = time.monotonic()

    for i, (call_id, token_address, call_ts) in enumerate(call_list):
        call_start = time.monotonic()

        try:
            bt_result, sl_result = backtest_call(
                client, token_address, call_ts
            )
            results.append((call_id, bt_result, sl_result))
        except Exception as e:
            log.error("Failed to price call %s (%s): %s", call_id, token_address[:10], e)
            from pricing.backtest import _unpriceable_pair
            bt_result, sl_result = _unpriceable_pair(str(e))
            results.append((call_id, bt_result, sl_result))

        call_elapsed = time.monotonic() - call_start
        total_elapsed = time.monotonic() - t1

        # Progress logging
        if (i + 1) % 10 == 0 or (i + 1) == total:
            rpm = (i + 1) / (total_elapsed / 60) if total_elapsed > 0 else 0
            eta = (total_elapsed / (i + 1)) * (total - i - 1) if i > 0 else 0
            log.info(
                "Progress: %d/%d (%.0f%%) | %.1f calls/min | ETA: %.0fs",
                i + 1, total, (i + 1) / total * 100, rpm, eta,
            )

        if progress_callback:
            progress_callback(i + 1, total)

    phase2_time = time.monotonic() - t1
    log.info(
        "Phase 2 complete: %d calls in %.1fs (%.1f calls/min)",
        total, phase2_time, total / (phase2_time / 60) if phase2_time > 0 else 0,
    )
    log.info(
        "Total: Phase 1=%.1fs + Phase 2=%.1fs = %.1fs (%.1f min)",
        phase1_time, phase2_time, phase1_time + phase2_time, (phase1_time + phase2_time) / 60,
    )

    return results


# ---- CLI Test Harness -----------------------------------------------------

if __name__ == "__main__":
    """Test on N calls from channel 26 (666 🔥 Calls)."""
    import sys
    sys.path.insert(0, ".")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    conn = get_connection()
    conn.row_factory = __import__("sqlite3").Row

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10

    calls = conn.execute("""
        SELECT id, token_address, call_timestamp
        FROM calls
        WHERE channel_id = 26
        ORDER BY call_timestamp ASC
        LIMIT ?
    """, (n,)).fetchall()

    print(f"Pricing {n} calls from channel 26 (666 🔥 Calls)...")
    print("=" * 70)

    t0 = time.monotonic()
    results = price_calls_v2(
        [{"token_address": r["token_address"], "call_timestamp": r["call_timestamp"]} for r in calls],
        progress_callback=lambda done, total: print(f"  [{done}/{total}]"),
    )
    elapsed = time.monotonic() - t0

    print("=" * 70)
    print(f"Total time: {elapsed:.1f}s ({elapsed/60:.1f} min)")
    print(f"Rate: {n / (elapsed/60):.1f} calls/min")
    print()

    wins = sum(1 for _, bt, _ in results if bt.is_win)
    losses = sum(1 for _, bt, _ in results if not bt.is_win)
    print(f"Results: {wins} wins, {losses} losses")

    for call_id, bt, sl in results:
        call_row = next(r for r in calls if r["id"] == call_id)
        mult = f"{bt.peak_profit_pct/100+1:.2f}x" if bt.peak_profit_pct else "N/A"
        print(f"  {call_id:4d} | {call_row['call_timestamp'][:16]} | {bt.status:5s} | {mult}")
