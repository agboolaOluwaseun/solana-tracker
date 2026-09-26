"""
pricing/strategy7d_cache.py — aggregate-aware cached fetchers for strategy7d.

Wraps a GeckoTerminalClientV2 (any network) + a SQLite connection on the unified
`price_cache` table (pool_address, token_address, aggregate, candle_ts, OHLCV)
and returns (fetch_hourly, fetch_minute) callables matching the injected-fetcher
contract of pricing/strategy7d.py.

Caching rules (frozen spec step 11, live-scoring amendments):
  - Minute candles: fully cached — re-runs are free while complete.
  - Hourly candles: fetched fresh every pass (screening needs the call-hour
    candle as current as possible), then MERGED over cached copies (fresh wins
    per timestamp). ALL candles including the call-hour candle are persisted:
    if GeckoTerminal later prunes a dead pool (shitty tokens get delisted and
    their candle history disappears), the finalization at day 7 still sees
    every candle we observed during the window — the verdict is locked from
    the best data captured, instead of collapsing to 'no call-hour candle'.
    Fresh candles overwrite the stored partial call-hour candle every pass,
    so the normal path stays spec-identical.
  - `client_v2.network` selects the chain network ('solana' | 'robinhood').
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, List, Optional, Tuple

from models import PricePoint
from pricing.geckoterminal_v2 import _strip_network_prefix, _parse_ohlcv

Fetcher = Callable[[str, str, datetime, datetime], Tuple[List[PricePoint], int]]


def _iso_ts(ts: datetime) -> str:
    return ts.replace(microsecond=0).isoformat() + "Z"


def _row_to_point(r) -> PricePoint:
    # Works with dict rows (db.py row_factory) AND plain tuples in the
    # SELECT's column order: candle_ts, open, high, low, close, volume.
    get = (lambda k, i: r[k]) if hasattr(r, "keys") else (lambda k, i: r[i])
    return PricePoint(
        timestamp=datetime.fromisoformat(get("candle_ts", 0).replace("Z", "")),
        open=get("open", 1), high=get("high", 2), low=get("low", 3),
        close=get("close", 4), volume=get("volume", 5),
    )


def make_cached_fetchers(conn, client_v2) -> Tuple[Fetcher, Fetcher]:
    """Return (fetch_hourly, fetch_minute) cached against price_cache.

    conn: open sqlite3 connection (row_factory rows are used; set it or pass a
    sqlite3.Connection — we read columns by name via integer fallback).
    client_v2: GeckoTerminalClientV2 instance; .network picks the chain.
    """

    def load_cached(pool: str, token: str, agg: str, start: datetime, end: datetime) -> List[PricePoint]:
        rows = conn.execute(
            "SELECT candle_ts, open, high, low, close, volume FROM price_cache "
            "WHERE pool_address=? AND token_address=? AND aggregate=? "
            "AND candle_ts >= ? AND candle_ts < ? ORDER BY candle_ts",
            (pool, token, agg, _iso_ts(start), _iso_ts(end)),
        ).fetchall()
        return [_row_to_point(r) for r in rows]

    def store_candles(pool: str, token: str, agg: str, candles: List[PricePoint]) -> None:
        for c in candles:
            conn.execute(
                "INSERT OR REPLACE INTO price_cache (pool_address, token_address, "
                "aggregate, candle_ts, open, high, low, close, volume) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (pool, token, agg, _iso_ts(c.timestamp), c.open, c.high, c.low, c.close, c.volume),
            )

    def _fetch_span(pool: str, token: str, agg: str, start: datetime, end: datetime) -> Tuple[List[PricePoint], int]:
        """One GT request (GT ignores after_timestamp; returns latest N before end)."""
        params = {
            "before_timestamp": int(end.replace(tzinfo=timezone.utc).timestamp()),
            "after_timestamp": int(start.replace(tzinfo=timezone.utc).timestamp()),
            "limit": 1000,
            "currency": "usd",
        }
        data = client_v2._request(
            f"/networks/{client_v2.network}/pools/{pool}/ohlcv/{agg}", params=params
        )
        candles = [c for c in _parse_ohlcv(data) if start <= c.timestamp < end]
        candles.sort(key=lambda c: c.timestamp)
        return candles, 1

    def fetch_minute(pool: str, token: str, start: datetime, end: datetime) -> Tuple[List[PricePoint], int]:
        """Minute candles for [start,end) with full caching (re-runs are free)."""
        pool = _strip_network_prefix(pool, client_v2.network)
        cached = load_cached(pool, token, "minute", start, end)
        expected = max(0, int((end - start).total_seconds() // 60))
        if cached and len(cached) >= expected:
            return cached, 0
        fresh, reqs = _fetch_span(pool, token, "minute", start, end)
        store_candles(pool, token, "minute", fresh)
        merged = {_iso_ts(c.timestamp): c for c in cached + fresh}
        out = [merged[k] for k in sorted(merged)]
        return out, reqs

    def fetch_hourly(pool: str, token: str, start: datetime, end: datetime) -> Tuple[List[PricePoint], int]:
        """Hourly candles for [start,end), fresh fetch UNION cached copies,
        then the fake-HIGH wick policy (pricing/wick_filter.py — user
        ruling 2026-09-24): suspect hours (high > WICK_HOUR_GATE x close)
        are verified against their minutes and repaired at READ time;
        innocent hours pass through untouched, so healthy calls pay zero
        extra API. Raw cache rows are never mutated; every pass re-derives
        from the API's truth. Filter errors fail open (raw series).

        Fresh wins on timestamp collisions; cached candles survive even
        when the API has already pruned them (dead-pool finalization), so
        a day-7 verdict uses everything ever observed in the window.
        """
        pool = _strip_network_prefix(pool, client_v2.network)

        def _wick(series: List[PricePoint]) -> Tuple[List[PricePoint], int]:
            try:
                from pricing.wick_filter import repair_hourly

                def _mins(a: datetime, b: datetime):
                    return fetch_minute(pool, token, a, b)

                return repair_hourly(series, _mins)
            except Exception:  # noqa: BLE001 — filter never breaks pricing
                return series, 0

        try:
            candles, reqs = _fetch_span(pool, token, "hour", start, end)
        except Exception:
            # API error (deleted pool often 404s): fall back to cached data
            # rather than losing a window we spent budget observing.
            cached = load_cached(pool, token, "hour", start, end)
            if cached:
                series, extra = _wick(cached)
                return series, 1 + extra
            raise
        if candles:
            store_candles(pool, token, "hour", candles)
        cached = load_cached(pool, token, "hour", start, end)
        if not cached:
            merged_series = candles
        else:
            merged = {_iso_ts(c.timestamp): c for c in cached + candles}
            merged_series = [merged[k] for k in sorted(merged)]
        series, extra = _wick(merged_series)
        return series, reqs + extra

    return fetch_hourly, fetch_minute
