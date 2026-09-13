"""
Re-score every call in a simulation DB from cached candles.
Computes BOTH normal (2x peak) and 50% stoploss strategies locally.
Zero API calls — reads price_cache only.
Usage: python scripts/rescore_channel.py <db_name> <channel_title>
"""
import sys
import sqlite3
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

DB_NAME = sys.argv[1] if len(sys.argv) > 1 else "simulation_666.db"
TITLE = sys.argv[2] if len(sys.argv) > 2 else "Channel"

SIM_DB = Path(__file__).parent.parent / DB_NAME
# autocommit (mirrors db.py); avoids BEGIN-in-transaction on re-runs
conn = sqlite3.connect(SIM_DB, isolation_level=None)
conn.row_factory = sqlite3.Row

from models import PricePoint
from pricing.backtest import score_candles, score_candles_stoploss

calls = conn.execute("""
    SELECT c.id, c.token_address, c.token_symbol, c.call_timestamp,
           c.entry_price_usd, c.peak_profit_pct, c.status,
           COALESCE(MAX(pc.pool_address), '') as pool_address
    FROM calls c
    LEFT JOIN price_cache pc ON pc.token_address = c.token_address
    WHERE c.status IN ('win', 'loss', 'unpriceable_loss')
    GROUP BY c.id
    ORDER BY c.call_timestamp ASC
""").fetchall()

print(f"Re-scoring {len(calls)} calls from cached candles (no API)...\n")

results = []
for call in calls:
    pool = call["pool_address"]
    candles_raw = conn.execute("""
        SELECT candle_ts, open, high, low, close, volume
        FROM price_cache
        WHERE token_address = ?
        ORDER BY candle_ts ASC
    """, (call["token_address"],)).fetchall()

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
        results.append({
            "id": call["id"], "symbol": call["token_symbol"] or "?",
            "time": call["call_timestamp"][:16], "mult": None,
            "normal_status": call["status"], "normal_win": False,
            "sl_status": call["status"], "sl_win": False, "sl_hit": False,
        })
        continue

    call_ts = datetime.fromisoformat(call["call_timestamp"].replace("Z", "+00:00")).replace(tzinfo=None)

    bt = score_candles(candles, call_ts, call["token_address"], pool or None)
    sl = score_candles_stoploss(candles, call_ts, call["token_address"], pool or None)

    results.append({
        "id": call["id"],
        "symbol": call["token_symbol"] or "?",
        "time": call["call_timestamp"][:16],
        "mult": (bt.peak_profit_pct or 0) / 100 + 1 if bt.peak_profit_pct else None,
        "normal_status": bt.status,
        "normal_win": bt.is_win,
        "sl_status": sl.status,
        "sl_win": sl.is_win,
        "sl_hit": sl.hit_stoploss,
    })

# Wins only count if they can actually win (exclude unpriceable from denominator)
priceable = [r for r in results if r["mult"] is not None]
N = len(priceable)
n_wins = sum(1 for r in priceable if r["normal_win"])
sl_wins = sum(1 for r in priceable if r["sl_win"])
sl_hits = sum(1 for r in priceable if r["sl_hit"])

print("=" * 90)
print(f"FULL REPORT — {TITLE} ({DB_NAME})")
print("=" * 90)
print()
print(f"Calls scored (priceable):  {N}")
print(f"Unpriceable:               {len(results) - N}")
print(f"  Normal strategy:       {n_wins} wins / {N - n_wins} losses = {n_wins/N*100:.1f}% WR" if N else "N/A")
print(f"  50% stoploss strategy: {sl_wins} wins / {N - sl_wins} losses = {sl_wins/N*100:.1f}% WR" if N else "N/A")
print(f"  Stops hit (-50%):      {sl_hits}")
print()

csv_path = Path(__file__).parent.parent / f"{Path(DB_NAME).stem}_full_report.csv"
with open(csv_path, "w") as f:
    f.write("Call ID,Symbol,Call Time,Peak Multiple,Normal Status,Stoploss Status,Stop Hit\n")
    for r in results:
        mult = f"{r['mult']:.4f}x" if r['mult'] else "N/A"
        f.write(f"{r['id']},{r['symbol']},{r['time']},{mult},{r['normal_status']},{r['sl_status']},{r['sl_hit']}\n")

print(f"CSV exported: {csv_path}")
print()

priceable.sort(key=lambda x: (x["mult"] or 0), reverse=True)
print(f"PER-CALL BREAKDOWN ({len(priceable)} priceable, by peak multiple desc):")
print("-" * 90)
print(f"{'ID':>4} {'Symbol':<14} {'Call Time':<17} {'Peak':>7}  {'Normal':<6} {'Stoploss':<9} {'Stop Hit'}")
print("-" * 90)
for r in priceable:
    ns = "WIN" if r["normal_win"] else "loss"
    ss = "WIN" if r["sl_win"] else "loss"
    hit = "yes" if r["sl_hit"] else "-"
    print(f"{r['id']:>4} {r['symbol']:<14} {r['time']:<17} {r['mult']:>6.2f}x  {ns:<6} {ss:<9} {hit}")

print()
print(f"\nSummary — Normal: {n_wins}/{N} ({n_wins/N*100:.1f}%) | Stoploss: {sl_wins}/{N} ({sl_wins/N*100:.1f}%)")