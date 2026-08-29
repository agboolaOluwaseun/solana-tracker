"""
Backtesting: score a single call against its 12-hour post-call price action.

Single 1-minute candle window — 12 hours starting 5 minutes before the call —
covers BOTH the exact call-minute entry price AND the peak scan. Rationale:
~99% of Solana shitcoins die within the first 12 hours, so a 7-day window only
adds API cost and noise. Reducing from the old two-phase (15-min minute fetch
+ 7-day hourly fetch) to a single 12h 1-minute fetch (~720 candles, one chunk)
cuts API calls roughly in half per token.

Cache-first + skip short-circuit: if a token was already priced by another
channel (present in token_meta) and the full 1-minute window for this call is
already in the price_cache, the API call is skipped entirely and the cached
candles are scored directly. Same-channel pump-updates are deduped earlier in
the pipeline (window-wide), so this skip only applies across channels.

Un-priceable calls (no pool, no candles, zero entry) count as a LOSS,
per the agreed rule (denominator = all calls).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from config import settings
from db import transaction
from models import BacktestResult, StoplossResult
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


def _unpriceable_pair(error: str, pool_address: Optional[str] = None):
    """Return a (BacktestResult, StoplossResult) pair both marked unpriceable."""
    r1 = BacktestResult(
        entry_price_usd=None, peak_price_usd=None, peak_timestamp=None,
        peak_profit_pct=None, is_win=False, status="unpriceable_loss",
        pool_address=pool_address, error=error,
    )
    r2 = StoplossResult(
        entry_price_usd=None, peak_price_usd=None, peak_timestamp=None,
        peak_profit_pct=None, hit_stoploss=False, stoploss_timestamp=None,
        is_win=False, status="unpriceable_loss", pool_address=pool_address, error=error,
    )
    return r1, r2


def backtest_call(
    client: GeckoTerminalClient,
    token_address: str,
    call_ts,
    peak_window_hours: Optional[int] = None,
) -> tuple[BacktestResult, StoplossResult]:
    """Score one call over its peak window; return BOTH strategies.

    Returns (normal_result, stoploss_result). Both are computed from the SAME
    single 1-minute candle fetch, so the 50% stop-loss strategy adds ZERO extra
    API cost. `call_ts` is a naive UTC datetime.
    Window = [call_ts - 5min, +peak_window_hours).
    Skip short-circuit: if the token was already priced by another channel and
    the call-minute candle is already cached, no API call is made.
    """
    window = peak_window_hours or settings.peak_window_hours
    entry_pad_minutes = 5
    start = call_ts - timedelta(minutes=entry_pad_minutes)
    end = start + timedelta(hours=window)

    # 1. Resolve pool (cache first). resolve_pool_smart auto-detects whether
    #    the address is a token mint OR a pool address.
    cached_pool = get_cached_pool(token_address)
    was_known = cached_pool is not None
    if cached_pool is None:
        try:
            resolved = client.resolve_pool_smart(token_address)
        except GeckoTerminalError as e:
            log.warning("resolve_pool_smart error for %s: %s", token_address, e)
            resolved = None
        if resolved is None or not resolved.get("pool_address"):
            return _unpriceable_pair("no pool")
        pool_info = {
            "token_address": resolved.get("token_address") or token_address,
            "pool_address": resolved["pool_address"],
            "symbol": resolved.get("symbol"),
            "name": resolved.get("name"),
            "liquidity_usd": resolved.get("liquidity_usd"),
        }
        upsert_token_meta(pool_info)
    else:
        pool_info = cached_pool

    pool_address = pool_info["pool_address"]

    # 2. Fetch 1-min candles for the full window (cache-first internally).
    # GeckoTerminalClient.fetch_ohlcv handles caching and gap-filling automatically.
    try:
        candles = client.fetch_ohlcv(
            pool_address, token_address, start, end, aggregate="minute",
        )
    except GeckoTerminalError as e:
        log.warning("minute fetch_ohlcv error for %s: %s", token_address, e)
        candles = []

    if not candles:
        return _unpriceable_pair("no candles", pool_address)

    return (
        score_candles(candles, call_ts, token_address, pool_address),
        score_candles_stoploss(candles, call_ts, token_address, pool_address),
    )


def backtest_call_birdeye(
    client,
    token_address: str,
    call_ts,
    peak_window_hours: Optional[int] = None,
) -> tuple[BacktestResult, StoplossResult]:
    """Score one call using Birdeye (fast alternative to GeckoTerminal).

    Birdeye takes the token address directly (no pool resolution), so each call
    is a single 12h x 1-min fetch at ~1 req/s. Candles are cached under the
    synthetic pool id 'birdeye' so re-runs skip the API call when the call-minute
    candle is already cached. Returns (normal, stoploss) results.
    """
    from pricing.cache import load_candles, store_candles

    window = peak_window_hours or settings.peak_window_hours
    start = call_ts - timedelta(minutes=5)
    end = start + timedelta(hours=window)

    cached = load_candles("birdeye", token_address, start, end)
    entry_cached = any(
        c.timestamp <= call_ts < c.timestamp + timedelta(minutes=1) for c in cached
    )
    if entry_cached:
        log.info(
            "SKIPPED Birdeye pricing %s (call-minute candle cached) - no API call",
            token_address[:12] + "...",
        )
        return (
            score_candles(cached, call_ts, token_address, "birdeye"),
            score_candles_stoploss(cached, call_ts, token_address, "birdeye"),
        )

    try:
        candles = client.fetch_ohlcv(token_address, start, end, interval="1m")
    except Exception as e:
        log.warning("Birdeye fetch_ohlcv error for %s: %s", token_address, e)
        candles = []

    if candles:
        store_candles("birdeye", token_address, candles, source="birdeye")
    else:
        return _unpriceable_pair("no candles (birdeye)", "birdeye")

    return (
        score_candles(candles, call_ts, token_address, "birdeye"),
        score_candles_stoploss(candles, call_ts, token_address, "birdeye"),
    )


def score_candles(
    candles: list,
    call_ts,
    token_address: str,
    pool_address: Optional[str] = None,
) -> BacktestResult:
    """
    Score a call using a pre-fetched list of candles (from any source).

    This is the shared scoring logic used by both GeckoTerminal and Birdeye
    backtest paths. `candles` should be a list of PricePoint objects.
    """
    if not candles:
        return BacktestResult(
            entry_price_usd=None, peak_price_usd=None, peak_timestamp=None,
            peak_profit_pct=None, is_win=False, status="unpriceable_loss",
            pool_address=pool_address, error="no candles provided",
        )

    sorted_candles = sorted(candles, key=lambda c: c.timestamp)

    # Entry: find the candle containing call_ts, use its open.
    # Try 1-minute candles first (most precise), then fall back to oldest candle.
    entry_price: Optional[float] = None
    
    # First pass: look for a candle whose 1-min window contains call_ts
    # (works for 1m candles from either GeckoTerminal or Birdeye).
    for c in sorted_candles:
        if c.timestamp <= call_ts < c.timestamp + timedelta(minutes=1):
            entry_price = c.open
            break
    
    # Second pass: if no 1m match, look for a candle whose wider window
    # contains call_ts (e.g. an hourly candle).  Use the candle's open.
    if entry_price is None:
        for c in sorted_candles:
            # Check the next candle's start to estimate this candle's end.
            idx = sorted_candles.index(c)
            if idx + 1 < len(sorted_candles):
                next_start = sorted_candles[idx + 1].timestamp
            else:
                next_start = call_ts + timedelta(hours=1)
            if c.timestamp <= call_ts < next_start:
                entry_price = c.open
                break

    # Fallback: use oldest candle's open.
    if entry_price is None:
        entry_price = sorted_candles[0].open

    if not entry_price or entry_price <= 0:
        return BacktestResult(
            entry_price_usd=None, peak_price_usd=None, peak_timestamp=None,
            peak_profit_pct=None, is_win=False, status="unpriceable_loss",
            pool_address=pool_address, error="zero entry price",
        )

    # Peak: max high across all candles.
    peak_candle = max(sorted_candles, key=lambda c: c.high)
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
        pool_address=pool_address or "unknown",
        candles_used=len(sorted_candles),
    )


def score_candles_stoploss(
    candles: list,
    call_ts,
    token_address: str,
    pool_address: Optional[str] = None,
    win_multiplier: float = 2.0,
    stoploss_pct: float = 50.0,
) -> StoplossResult:
    """
    Score a call using the 50% stop-loss strategy.
    Chronologically tracks 1m candles to see if the win target (2x) or loss threshold (-50%) is hit first.
    """
    if not candles:
        return StoplossResult(
            entry_price_usd=None, peak_price_usd=None, peak_timestamp=None,
            peak_profit_pct=None, hit_stoploss=False, stoploss_timestamp=None,
            is_win=False, status="unpriceable_loss", pool_address=pool_address, error="no candles provided",
        )

    sorted_candles = sorted(candles, key=lambda c: c.timestamp)
    
    # Entry price
    entry_price = None
    for c in sorted_candles:
        if c.timestamp <= call_ts < c.timestamp + timedelta(minutes=1):
            entry_price = c.open
            break
    if entry_price is None:
        for c in sorted_candles:
            if c.timestamp <= call_ts:
                entry_price = c.close
        if entry_price is None and sorted_candles:
            entry_price = sorted_candles[0].open
            
    if not entry_price or entry_price <= 0:
        return StoplossResult(
            entry_price_usd=None, peak_price_usd=None, peak_timestamp=None,
            peak_profit_pct=None, hit_stoploss=False, stoploss_timestamp=None,
            is_win=False, status="unpriceable_loss", pool_address=pool_address, error="zero entry price",
        )

    win_target = entry_price * win_multiplier
    loss_threshold = entry_price * (1.0 - stoploss_pct / 100.0)

    hit_win = False
    hit_loss = False
    win_ts = None
    loss_ts = None
    # full_max tracks the ENTIRE window's max high. A WIN reports this same
    # peak as the normal strategy, even if the token kept pumping after 2x.
    full_max = entry_price
    full_max_ts = call_ts
    # max_before_stop tracks the best price seen BEFORE a stop-out. A LOSS due
    # to the -50% stop reports the peak up to the moment of the stop.
    max_before_stop = entry_price
    max_before_stop_ts = call_ts

    for c in sorted_candles:
        if c.timestamp < call_ts:
            continue

        if c.high > full_max:
            full_max = c.high
            full_max_ts = c.timestamp

        if not hit_win and not hit_loss:
            # Still racing: 2x target vs -50% stop.
            if c.high > max_before_stop:
                max_before_stop = c.high
                max_before_stop_ts = c.timestamp

            if not hit_loss and c.low <= loss_threshold:
                hit_loss = True
                loss_ts = c.timestamp
            if not hit_win and c.high >= win_target:
                hit_win = True
                win_ts = c.timestamp

            # If both hit in the same candle, guess order based on candle direction.
            if hit_loss and hit_win and loss_ts == win_ts:
                if c.close >= c.open:
                    hit_loss = False  # Green candle: went up then down
                else:
                    hit_win = False   # Red candle: went down then up

        if hit_loss:
            break  # stop-out locks the peak to max_before_stop
        # On a win, keep scanning the rest of the window so the reported peak
        # is the full-window max high (same value the normal strategy reports),
        # not just the value at the moment 2x was reached.

    if hit_win:
        peak, peak_ts = full_max, full_max_ts
        status, is_win, hit_sl, sl_ts = "win", True, False, None
    elif hit_loss:
        peak, peak_ts = max_before_stop, max_before_stop_ts
        status, is_win, hit_sl, sl_ts = "loss", False, True, loss_ts
    else:
        # Neither hit within the time window -> a loss (didn't reach 2x).
        peak, peak_ts = full_max, full_max_ts
        status, is_win, hit_sl, sl_ts = "loss", False, False, None

    return StoplossResult(
        entry_price_usd=entry_price, peak_price_usd=peak, peak_timestamp=peak_ts,
        peak_profit_pct=(peak / entry_price - 1.0) * 100.0,
        hit_stoploss=hit_sl, stoploss_timestamp=sl_ts, is_win=is_win,
        status=status, pool_address=pool_address, candles_used=len(sorted_candles),
    )
