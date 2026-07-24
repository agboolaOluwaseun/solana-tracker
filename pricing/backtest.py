"""
Backtesting: score a single call against its 7-day post-call price action.

Two-phase pricing strategy (fast + accurate):
  1. MINUTE candles — tight 30-min window around the call timestamp → exact
     entry price at the call minute (memecoins move 5x+ per minute).
  2. HOUR candles — full 7-day window → peak price scan over the full window.

This reduces API calls from ~11 (all-minute, 7-day) to ~2 per token while
giving MORE accurate results (minute entry vs hourly entry).

Un-priceable calls (no pool, no candles, zero entry) count as a LOSS,
per the agreed rule (denominator = all calls).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from config import settings
from db import transaction
from models import BacktestResult
from pricing.geckoterminal import GeckoTerminalClient, GeckoTerminalError

log = logging.getLogger(__name__)


def upsert_token_meta(meta: dict) -> None:
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
                "address": meta["token_address"],
                "symbol": meta.get("symbol"),
                "name": meta.get("name"),
                "dex_pool_id": meta.get("pool_address"),
                "liquidity_usd": meta.get("liquidity_usd"),
            },
        )


def get_cached_pool(token_address: str) -> Optional[dict]:
    conn = __import__("db").get_connection()
    row = conn.execute(
        "SELECT dex_pool_id, symbol, name, liquidity_usd FROM token_meta WHERE address = ?",
        (token_address,),
    ).fetchone()
    if row and row["dex_pool_id"]:
        # Defensive: strip any legacy 'solana_' prefix from cached rows so the
        # OHLCV path never doubles the network segment.
        from pricing.geckoterminal import _strip_network_prefix
        from config import settings
        bare = _strip_network_prefix(row["dex_pool_id"], settings.solana_network)
        return {
            "token_address": token_address,
            "pool_address": bare,
            "symbol": row["symbol"],
            "name": row["name"],
            "liquidity_usd": row["liquidity_usd"],
        }
    return None


def backtest_call(
    client: GeckoTerminalClient,
    token_address: str,
    call_ts,
    peak_window_hours: Optional[int] = None,
) -> BacktestResult:
    """Score one call. `call_ts` is a naive UTC datetime."""
    window = peak_window_hours or settings.peak_window_hours
    end_ts = call_ts + timedelta(hours=window)

    # 1. Resolve pool (cache first). resolve_pool_smart auto-detects whether
    #    the address is a token mint OR a pool address.
    pool_info = get_cached_pool(token_address)
    if pool_info is None:
        try:
            resolved = client.resolve_pool_smart(token_address)
        except GeckoTerminalError as e:
            log.warning("resolve_pool_smart error for %s: %s", token_address, e)
            resolved = None
        if resolved is None or not resolved.get("pool_address"):
            return BacktestResult(
                entry_price_usd=None, peak_price_usd=None, peak_timestamp=None,
                peak_profit_pct=None, is_win=False, status="unpriceable_loss",
                error="no pool",
            )
        pool_info = {
            "token_address": resolved.get("token_address") or token_address,
            "pool_address": resolved["pool_address"],
            "symbol": resolved.get("symbol"),
            "name": resolved.get("name"),
            "liquidity_usd": resolved.get("liquidity_usd"),
        }
        upsert_token_meta(pool_info)

    pool_address = pool_info["pool_address"]

    # ---- PHASE 1: Minute candles for exact entry price ----
    # Tight window: 5 min before call to 10 min after = 15 candles max.
    # This gives us the open of the EXACT minute the call was posted.
    entry_pad_minutes = 5
    entry_window_after = 10
    minute_start = call_ts - timedelta(minutes=entry_pad_minutes)
    minute_end = call_ts + timedelta(minutes=entry_window_after)

    entry_price: Optional[float] = None
    entry_candle_ts = None

    try:
        minute_candles = client.fetch_ohlcv(
            pool_address, token_address, minute_start, minute_end,
            aggregate="minute",
        )
    except GeckoTerminalError as e:
        log.warning("minute fetch_ohlcv error for %s: %s", token_address, e)
        minute_candles = []

    if minute_candles:
        # Sort ascending; find the candle containing call_ts.
        minute_sorted = sorted(minute_candles, key=lambda c: c.timestamp)
        for c in minute_sorted:
            if c.timestamp <= call_ts < c.timestamp + timedelta(minutes=1):
                entry_price = c.open
                entry_candle_ts = c.timestamp
                break
        # Fallback: use the oldest candle's open if no exact match.
        if entry_price is None and minute_sorted:
            entry_price = minute_sorted[0].open
            entry_candle_ts = minute_sorted[0].timestamp

    # ---- PHASE 2: Hourly candles for 7-day peak scan ----
    try:
        hour_candles = client.fetch_ohlcv(
            pool_address, token_address, call_ts, end_ts,
            aggregate="hour",
        )
    except GeckoTerminalError as e:
        log.warning("hour fetch_ohlcv error for %s: %s", token_address, e)
        hour_candles = []

    # If we got neither minute nor hour candles, it's unpriceable.
    if not minute_candles and not hour_candles:
        return BacktestResult(
            entry_price_usd=None, peak_price_usd=None, peak_timestamp=None,
            peak_profit_pct=None, is_win=False, status="unpriceable_loss",
            pool_address=pool_address, error="no candles (both phases)",
        )

    # ---- Combine results ----
    all_candles = list(minute_candles) + list(hour_candles)
    candles_sorted = sorted(all_candles, key=lambda c: c.timestamp)

    # Entry: prefer minute candle; fallback to oldest hourly candle's open.
    if entry_price is None and candles_sorted:
        entry_price = candles_sorted[0].open
        entry_candle_ts = candles_sorted[0].timestamp

    if not entry_price or entry_price <= 0:
        return BacktestResult(
            entry_price_usd=None, peak_price_usd=None, peak_timestamp=None,
            peak_profit_pct=None, is_win=False, status="unpriceable_loss",
            pool_address=pool_address, error="zero entry price",
        )

    # Peak: max high across ALL candles (minute + hourly).
    peak_candle = max(all_candles, key=lambda c: c.high)
    peak = peak_candle.high

    peak_profit_pct = (peak / entry_price - 1.0) * 100.0
    is_win = peak >= settings.win_multiplier * entry_price
    status = "win" if is_win else "loss"

    return BacktestResult(
        entry_price_usd=entry_price,
        peak_price_usd=peak,
        peak_timestamp=peak_candle.timestamp,
        peak_profit_pct=peak_profit_pct,
        is_win=is_win,
        status=status,
        pool_address=pool_address,
        candles_used=len(all_candles),
    )
