"""
Persistent price cache backed by the `price_cache` SQLite table.

Every candle GeckoTerminal returns is stored so re-runs cost zero API calls.
The cache is keyed by (pool_address, token_address, candle_ts).
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from db import transaction
from models import PricePoint


def _iso(ts: datetime) -> str:
    return ts.replace(microsecond=0).isoformat() + "Z"


def store_candles(
    pool_address: str,
    token_address: str,
    candles: List[PricePoint],
    source: str = "geckoterminal",
) -> int:
    """Upsert a batch of candles. Returns the number written."""
    if not candles:
        return 0
    rows = [
        (
            pool_address,
            token_address,
            _iso(c.timestamp),
            c.open,
            c.high,
            c.low,
            c.close,
            c.volume,
            source,
        )
        for c in candles
    ]
    with transaction() as conn:
        conn.executemany(
            """
            INSERT INTO price_cache
                (pool_address, token_address, candle_ts, open, high, low, close, volume, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pool_address, token_address, candle_ts) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, volume=excluded.volume,
                source=excluded.source, cached_at=datetime('now')
            """,
            rows,
        )
    return len(rows)


def load_candles(
    pool_address: str,
    token_address: str,
    start: datetime,
    end: datetime,
) -> List[PricePoint]:
    """Load cached candles in [start, end), sorted ascending."""
    conn = __import__("db").get_connection()
    cur = conn.execute(
        """
        SELECT candle_ts, open, high, low, close, volume
        FROM price_cache
        WHERE pool_address = ? AND token_address = ?
          AND candle_ts >= ? AND candle_ts < ?
        ORDER BY candle_ts ASC
        """,
        (pool_address, token_address, _iso(start), _iso(end)),
    )
    out: List[PricePoint] = []
    for r in cur.fetchall():
        ts = datetime.fromisoformat(r["candle_ts"].replace("Z", ""))
        out.append(
            PricePoint(
                timestamp=ts,
                open=r["open"],
                high=r["high"],
                low=r["low"],
                close=r["close"],
                volume=r["volume"],
            )
        )
    return out
