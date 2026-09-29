"""Persistent price cache backed by the `price_cache` SQLite table.

TOKEN-PRIMARY (user ruling 2026-09-28, migration in
scripts/migrate_price_cache_token_primary.py): a candle belongs to a TOKEN.
PRIMARY KEY (chain, token_address, aggregate, candle_ts); `pool_address` is
demoted to a provenance stamp (NULL for Birdeye-sourced series), `source`
says which API produced the row. One honest curve per token per aggregate —
the pool-keyed era let a flipped pair's curve masquerade as the token's own
(museic/META, DRUGS/BIO); the key itself now forbids that.

Reads filter on (token, aggregate[, chain]); chain=None matches every chain
(a token address is effectively chain-unique in practice — 1 cross-chain
duplicate in the whole DB — and the extra filter would need a chain the
caller may not know yet, e.g. before a rescue correction).
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from db import get_connection, transaction
from models import PricePoint

# GT network slug -> internal chain slug (calls.chain namespace)
NETWORK_TO_CHAIN = {"solana": "sol", "robinhood": "robinhood", "eth": "eth",
                    "ethereum": "eth", "bsc": "bsc", "base": "base",
                    "arc": "arc"}

AGG_MIN = {"minute", "1m", "1"}      # GT has used several spellings
AGG_HOUR = {"hour", "2h", "4h"}


def norm_agg(aggregate: str) -> str:
    a = (aggregate or "hour").lower()
    if a in AGG_MIN:
        return "minute"
    if a == "day":
        return "day"
    return "hour"


def _iso(ts: datetime) -> str:
    return ts.replace(microsecond=0).isoformat() + "Z"


def chain_for(network: str) -> str:
    return NETWORK_TO_CHAIN.get((network or "solana").lower(),
                                (network or "sol").lower())


def store_candles(
    token_address: str,
    aggregate: str,
    candles: List[PricePoint],
    *,
    chain: str = "sol",
    pool_address: Optional[str] = None,
    source: str = "geckoterminal",
    conn=None,
) -> int:
    """Upsert a batch of candles for one token at one granularity.

    conn: an open sqlite connection whose transaction the CALLER commits
    (the cached fetchers run inside a caller-owned conn). Default None:
    open our own connection + commit per batch. Returns rows written.
    """
    if not candles:
        return 0
    agg = norm_agg(aggregate)
    rows = [
        (chain, token_address, agg, _iso(c.timestamp), pool_address, source,
         c.open, c.high, c.low, c.close, c.volume)
        for c in candles
    ]
    if conn is None:
        with transaction() as c2:
            _insert(c2, rows)
    else:
        _insert(conn, rows)
    return len(rows)


def _insert(conn, rows) -> None:
    conn.executemany(
        """
        INSERT INTO price_cache
            (chain, token_address, aggregate, candle_ts, pool_address, source,
             open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chain, token_address, aggregate, candle_ts) DO UPDATE SET
            pool_address=excluded.pool_address, source=excluded.source,
            open=excluded.open, high=excluded.high, low=excluded.low,
            close=excluded.close, volume=excluded.volume,
            cached_at=datetime('now')
        """,
        rows,
    )


def load_candles(
    token_address: str,
    aggregate: str,
    start: datetime,
    end: datetime,
    *,
    chain: Optional[str] = None,
    conn=None,
) -> List[PricePoint]:
    """Cached candles for one token+aggregate in [start, end), ascending.

    Freshness is by candle_ts, not cached_at: upserts on the same key
    replace the row, so this returns the freshest observation per timestamp
    regardless of source (a GT row refreshed at day-7 supersedes an older
    Birdeye row for the same hour; different hours coexist).
    """
    c = conn or get_connection()
    agg = norm_agg(aggregate)
    q = """
        SELECT candle_ts, open, high, low, close, volume
        FROM price_cache
        WHERE token_address = ? AND aggregate = ?
          AND candle_ts >= ? AND candle_ts < ?
    """
    params: list = [token_address, agg, _iso(start), _iso(end)]
    if chain is not None:
        q += " AND chain = ?"
        params.append(chain)
    q += " ORDER BY candle_ts ASC"
    cur = c.execute(q, params)
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
