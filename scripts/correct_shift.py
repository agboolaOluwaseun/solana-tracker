"""
Correct the off-by-one pool/score shift in simulation DBs.

price_calls_v2 previously dropped the call `id` and used the enumerate index,
so every calls row got the NEXT token's pool/entry/peak/status written to it.
token_meta + price_cache were keyed by token address and are CORRECT.

This script rewrites each calls row from the cached candles (zero API),
restoring the true per-call results, and prints the corrected summary.

Usage: python scripts/correct_shift.py <db1> [db2 ...]
"""
import sys
import sqlite3
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import PricePoint
from pricing.backtest import score_candles, score_candles_stoploss


def correct_db(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    print(f"\n{'='*80}\nCorrecting {db_path.name}\n{'='*80}")

    # Find the true pool per token from price_cache + token_meta (correct sources)
    calls = conn.execute("""
        SELECT DISTINCT c.id, c.token_address, c.call_timestamp
        FROM calls c ORDER BY c.id
    """).fetchall()

    # Pre-load the canonical pool per token (from token_meta, the correct phase-1 source)
    meta = {
        r["address"]: r["dex_pool_id"]
        for r in conn.execute("SELECT address, dex_pool_id FROM token_meta").fetchall()
    }

    updated = 0
    wins_n, losses_n, unpriceable_n = 0, 0, 0
    sl_wins_n, sl_hits = 0, 0

    for call in calls:
        token = call["token_address"]
        candles_raw = conn.execute("""
            SELECT candle_ts, open, high, low, close, volume
            FROM price_cache WHERE token_address = ?
            ORDER BY candle_ts ASC
        """, (token,)).fetchall()

        candles = []
        for r in candles_raw:
            try:
                ts = datetime.fromisoformat(r["candle_ts"].replace("Z", "+00:00")).replace(tzinfo=None)
                candles.append(PricePoint(
                    timestamp=ts, open=r["open"], high=r["high"],
                    low=r["low"], close=r["close"], volume=r["volume"] or 0,
                ))
            except (ValueError, TypeError):
                continue

        if not candles:
            # No cached candles -> genuinely unpriceable (dead token / no pool)
            conn.execute("""
                UPDATE calls SET status='unpriceable_loss', entry_price_usd=NULL,
                    peak_price_usd=NULL, peak_profit_pct=NULL, is_win=0,
                    pool_address=COALESCE(?, pool_address), priced_at=datetime('now')
                WHERE id=?
            """, (meta.get(token), call["id"]))
            unpriceable_n += 1
            updated += 1
            continue

        try:
            call_ts = datetime.fromisoformat(call["call_timestamp"].replace("Z", "+00:00")).replace(tzinfo=None)
        except (ValueError, TypeError):
            continue

        pool = meta.get(token)
        bt = score_candles(candles, call_ts, token, pool)
        sl = score_candles_stoploss(candles, call_ts, token, pool)

        conn.execute("""
            UPDATE calls SET
                entry_price_usd=?, peak_price_usd=?, peak_timestamp=?,
                peak_profit_pct=?, is_win=?, status=?, pool_address=?,
                priced_at=datetime('now')
            WHERE id=?
        """, (
            bt.entry_price_usd, bt.peak_price_usd,
            bt.peak_timestamp.isoformat() if bt.peak_timestamp else None,
            bt.peak_profit_pct, 1 if bt.is_win else 0, bt.status,
            bt.pool_address or pool, call["id"],
        ))
        updated += 1
        if bt.is_win:
            wins_n += 1
        else:
            losses_n += 1
        if sl.is_win:
            sl_wins_n += 1
        if sl.hit_stoploss:
            sl_hits += 1

    conn.commit()

    decided = wins_n + losses_n
    print(f"Calls corrected: {updated}")
    print(f"  Normal:      {wins_n} wins / {losses_n} losses / {unpriceable_n} unpriceable"
          f" = {wins_n/decided*100:.1f}% WR" if decided else "  no decided calls")
    print(f"  Stoploss:    {sl_wins_n} wins / {decided - sl_wins_n} losses"
          f" = {sl_wins_n/decided*100:.1f}% WR, {sl_hits} stops hit")

    # Export corrected CSV
    csv_path = db_path.parent / f"{db_path.stem}_corrected.csv"
    rows = conn.execute("""
        SELECT id, token_symbol, call_timestamp, peak_profit_pct, is_win, status
        FROM calls ORDER BY (peak_profit_pct / 100 + 1) DESC
    """).fetchall()
    with open(csv_path, "w") as f:
        f.write("Call ID,Symbol,Call Time,Peak Multiple,Status\n")
        for r in rows:
            if r["peak_profit_pct"] is not None:
                f.write(f"{r['id']},{r['token_symbol'] or '?'},{r['call_timestamp'][:16]},"
                        f"{r['peak_profit_pct']/100+1:.4f}x,{r['status']}\n")
    print(f"CSV: {csv_path}")
    conn.close()
    return wins_n, losses_n, unpriceable_n, sl_wins_n, sl_hits


if __name__ == "__main__":
    dbs = sys.argv[1:] or ["simulation_666.db", "simulation_trenches.db"]
    for name in dbs:
        p = Path(__file__).parent.parent / name
        if p.exists():
            correct_db(p)
        else:
            print(f"skip {name}: not found")