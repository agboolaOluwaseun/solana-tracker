"""
One-shot repair: fetch candles for the 19 calls that have no cached data
(8 unpriceable stoploss + 11 priced-without-cache), then overwrite both
the normal strategy result (calls table) and stoploss result (stoploss_results).

Tries GeckoTerminal first (pool resolution + 1m candles), then falls back
to Birdeye (token-address directly, no pool needed) if GeckoTerminal fails.
"""
import sys, time
from datetime import datetime, timedelta

from config import settings
from db import get_connection, init_db
from pipeline import init_stoploss_table
from pricing.geckoterminal import GeckoTerminalClient, GeckoTerminalError
from pricing.birdeye import BirdeyeClient, BirdeyeError
from pricing.backtest import score_candles, score_candles_stoploss
from pricing.cache import store_candles


# ── helpers ────────────────────────────────────────────────────────────────
def _iso(dt):
    return dt.isoformat() + "Z" if dt else None


def _try_gecko(client, token_address, call_ts, end_ts, symbol, call_id):
    """Try GeckoTerminal: resolve pool, fetch 1m candles. Returns (candles, pool_addr) or (None, None)."""
    try:
        resolved = client.resolve_pool_smart(token_address)
    except GeckoTerminalError as e:
        print(f"  call {call_id} {symbol or '?'}: gecko pool resolution failed — {e}")
        return None, None

    if resolved is None:
        print(f"  call {call_id} {symbol or '?'}: gecko — no pool found")
        return None, None

    pool_addr = resolved["pool_address"]
    try:
        fetched = client.fetch_ohlcv(
            pool_addr, token_address, call_ts, end_ts, aggregate="minute",
        )
    except GeckoTerminalError as e:
        print(f"  call {call_id} {symbol or '?'}: gecko fetch failed — {e}")
        return None, None

    if not fetched:
        print(f"  call {call_id} {symbol or '?'}: gecko — no candles returned")
        return None, None

    return fetched, pool_addr


def _try_birdeye(birdeye_client, token_address, call_ts, end_ts, symbol, call_id):
    """Try Birdeye: token address directly, fetch 1m candles. Returns (candles, pool_addr) or (None, None)."""
    try:
        fetched = birdeye_client.fetch_ohlcv(
            token_address, call_ts, end_ts, interval="1m",
        )
    except (BirdeyeError, Exception) as e:
        print(f"  call {call_id} {symbol or '?'}: birdeye fetch failed — {e}")
        return None, None

    if not fetched:
        print(f"  call {call_id} {symbol or '?'}: birdeye — no candles returned")
        return None, None

    return fetched, "birdeye"


def repair_one(conn, gecko_client, birdeye_client, call_id, token_address, symbol, call_ts):
    """Fetch, score, and overwrite both strategies for one call.
    GeckoTerminal first, Birdeye fallback."""
    window = settings.peak_window_hours
    end_ts = call_ts + timedelta(hours=window)

    # 1. Try GeckoTerminal
    candles, pool_addr = _try_gecko(gecko_client, token_address, call_ts, end_ts, symbol, call_id)

    # 2. Fallback to Birdeye
    if candles is None:
        print(f"  call {call_id} {symbol or '?'}: falling back to Birdeye...")
        candles, pool_addr = _try_birdeye(birdeye_client, token_address, call_ts, end_ts, symbol, call_id)

    if candles is None:
        print(f"  call {call_id} {symbol or '?'}: BOTH sources failed — unpriceable")
        return "both_failed"

    # 3. Cache the candles
    source = "geckoterminal" if pool_addr != "birdeye" else "birdeye"
    store_candles(pool_addr, token_address, candles, source=source)

    # 4. Score both strategies
    normal = score_candles(candles, call_ts, token_address, pool_addr)
    stoploss = score_candles_stoploss(
        candles, call_ts, token_address, pool_addr,
        win_multiplier=2.0, stoploss_pct=50.0,
    )

    # 5. Update calls table
    conn.execute(
        """UPDATE calls SET
           entry_price_usd = ?, peak_price_usd = ?, peak_timestamp = ?,
           peak_profit_pct = ?, peak_multiple = ?,
           is_win = ?, status = ?, pool_address = ?, priced_at = datetime('now')
           WHERE id = ?""",
        (
            normal.entry_price_usd,
            normal.peak_price_usd,
            _iso(normal.peak_timestamp) if normal.peak_timestamp else None,
            normal.peak_profit_pct,
            (normal.peak_price_usd / normal.entry_price_usd)
            if normal.entry_price_usd and normal.peak_price_usd else None,
            1 if normal.is_win else 0,
            normal.status,
            pool_addr,
            call_id,
        ),
    )

    # 6. Upsert stoploss_results
    conn.execute(
        """INSERT INTO stoploss_results
        (call_id, entry_price_usd, peak_price_usd, peak_timestamp,
         peak_profit_pct, hit_stoploss, stoploss_timestamp,
         is_win, status, error, computed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(call_id) DO UPDATE SET
        entry_price_usd=excluded.entry_price_usd,
        peak_price_usd=excluded.peak_price_usd,
        peak_timestamp=excluded.peak_timestamp,
        peak_profit_pct=excluded.peak_profit_pct,
        hit_stoploss=excluded.hit_stoploss,
        stoploss_timestamp=excluded.stoploss_timestamp,
        is_win=excluded.is_win,
        status=excluded.status,
        error=excluded.error,
        computed_at=datetime('now')""",
        (
            call_id,
            stoploss.entry_price_usd,
            stoploss.peak_price_usd,
            _iso(stoploss.peak_timestamp) if stoploss.peak_timestamp else None,
            stoploss.peak_profit_pct,
            1 if stoploss.hit_stoploss else 0,
            _iso(stoploss.stoploss_timestamp) if stoploss.stoploss_timestamp else None,
            1 if stoploss.is_win else 0,
            stoploss.status,
            stoploss.error,
        ),
    )

    print(f"    source={source}  normal={normal.status} peak={normal.peak_profit_pct:.1f}%  "
          f"sl={stoploss.status} hit_sl={stoploss.hit_stoploss}")
    return "ok"


# ── main ───────────────────────────────────────────────────────────────────
def main():
    init_db()
    init_stoploss_table()
    conn = get_connection()
    gecko_client = GeckoTerminalClient()
    birdeye_client = BirdeyeClient()

    rows = conn.execute("""
        SELECT c.id, c.channel_id, c.token_symbol, c.token_address, c.call_timestamp
        FROM calls c
        JOIN stoploss_results sr ON sr.call_id = c.id
        WHERE sr.status = 'unpriceable_loss'
        UNION
        SELECT c.id, c.channel_id, c.token_symbol, c.token_address, c.call_timestamp
        FROM calls c
        WHERE c.status IN ('win','loss')
          AND c.entry_price_usd IS NOT NULL
          AND c.token_address NOT IN (SELECT DISTINCT token_address FROM price_cache)
        ORDER BY channel_id, id
    """).fetchall()

    print(f"Repairing {len(rows)} calls (GeckoTerminal first, Birdeye fallback)...\n")

    results = {"ok": 0, "both_failed": 0}
    for r in rows:
        call_id = r["id"]
        sym = r["token_symbol"]
        addr = r["token_address"]
        ts_str = r["call_timestamp"]
        call_ts = datetime.fromisoformat(ts_str.replace("Z", ""))
        outcome = repair_one(conn, gecko_client, birdeye_client, call_id, addr, sym, call_ts)
        results[outcome] = results.get(outcome, 0) + 1
        conn.commit()
        time.sleep(0.6)

    print(f"\nDone: {results}")


if __name__ == "__main__":
    main()