"""
pricing/strategy7d_cache.py — aggregate-aware cached fetchers for strategy7d.

Wraps a GeckoTerminalClientV2 (any network) + a SQLite connection on the unified
`price_cache` table (pool_address, token_address, aggregate, candle_ts, OHLCV)
and returns (fetch_hourly, fetch_minute) callables matching the injected-fetcher
contract of pricing/strategy7d.py.

Caching rule (frozen spec step 11):
  - Minute candles: fully cached — re-runs are free.
  - Hourly candles: cached EXCEPT the call-hour candle (the window's first
    candle). It is ambiguous (contains pre-call movement), never trusted for
    the final path, and never persisted; screening always refetches it, so a
    re-run costs exactly one hourly request per call.
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
    return PricePoint(
        timestamp=datetime.fromisoformat(r["candle_ts"].replace("Z", "")),
        open=r["open"], high=r["high"], low=r["low"],
        close=r["close"], volume=r["volume"],
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
        """Hourly candles for [start,end). Cache stores ALL EXCEPT the first
        (call-hour) candle — screening always needs it fresh, so re-runs keep
        costing exactly one hourly request per call."""
        pool = _strip_network_prefix(pool, client_v2.network)
        candles, reqs = _fetch_span(pool, token, "hour", start, end)
        # persist everything except the first (call-hour) candle
        if candles:
            store_candles(pool, token, "hour", candles[1:])
        return candles, reqs

    return fetch_hourly, fetch_minute
