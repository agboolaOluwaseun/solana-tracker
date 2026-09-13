"""
Re-score every call in simulation_666.db from cached candles.
Computes BOTH the normal (2x peak) and 50% stoploss strategies locally.
Zero API calls — reads price_cache only.
"""
import sys
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

SIM_DB = Path(__file__).parent.parent / "simulation_666.db"
conn = sqlite3.connect(SIM_DB)
conn.row_factory = sqlite3.Row

from models import PricePoint
from pricing.backtest import score_candles, score_candles_stoploss

# Load all calls with entry info
calls = conn.execute("""
    SELECT c.id, c.token_address, c.token_symbol, c.call_timestamp,
           c.entry_price_usd, c.peak_profit_pct, c.status,
           COALESCE(MAX(pc.pool_address), '') as pool_address
    FROM calls c
    LEFT JOIN price_cache pc ON pc.token_address = c.token_address
    WHERE c.status IN ('win', 'loss') 
    GROUP BY c.id
    ORDER BY c.call_timestamp ASC
""").fetchall()

print(f"Re-scoring {len(calls)} calls from cached candles (no API)...\n")

results = []
for call in calls:
    # Load cached candles for this token (any pool, but prefer the one used)
    pool = call["pool_address"]
    candles_raw = conn.execute("""
        SELECT candle_ts, open, high, low, close, volume
        FROM price_cache
        WHERE token_address = ?
        ORDER BY candle_ts ASC
    """, (call["token_address"],)).fetchall()
    
    # Build PricePoint list
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
        continue
    
    call_ts = datetime.fromisoformat(call["call_timestamp"].replace("Z", "+00:00")).replace(tzinfo=None)
    
    # Score BOTH strategies from the same candles
    bt = score_candles(candles, call_ts, call["token_address"], pool or None)
    sl = score_candles_stoploss(candles, call_ts, call["token_address"], pool or None)
    
    results.append({
        "id": call["id"],
        "symbol": call["token_symbol"] or "?",
        "time": call["call_timestamp"][:16],
        "mult": (bt.peak_profit_pct or 0) / 100 + 1,
        "normal_status": bt.status,
        "normal_win": bt.is_win,
        "sl_status": sl.status,
        "sl_win": sl.is_win,
        "sl_hit": sl.hit_stoploss,
    })

# Sort by multiplier descending
results.sort(key=lambda x: x["mult"], reverse=True)

N = len(results)
n_wins = sum(1 for r in results if r["normal_win"])
n_losses = N - n_wins
sl_wins = sum(1 for r in results if r["sl_win"])
sl_losses = N - sl_wins
sl_hits = sum(1 for r in results if r["sl_hit"])

print("=" * 90)
print("FULL REPORT — 666 🔥 Calls (April 1 → now), re-scored from cache")
print("=" * 90)
print()
print(f"Calls scored:            {N}")
print(f"  Normal strategy:       {n_wins} wins / {n_losses} losses = {n_wins/N*100:.1f}% WR" if N else "N/A")
print(f"  50% stoploss strategy: {sl_wins} wins / {sl_losses} losses = {sl_wins/N*100:.1f}% WR" if N else "N/A")
print(f"  Stops hit (-50%):      {sl_hits}")
print()

# CSV for Google Sheets
csv_path = Path(__file__).parent.parent / "simulation_666_full_report.csv"
with open(csv_path, "w") as f:
    f.write("Call ID,Symbol,Call Time,Peak Multiple,Normal Status,Stoploss Status,Stop Hit\n")
    for r in results:
        f.write(f"{r['id']},{r['symbol']},{r['time']},{r['mult']:.4f}x,{r['normal_status']},{r['sl_status']},{r['sl_hit']}\n")

print(f"CSV exported: {csv_path}")
print()

# Per-call table
print("PER-CALL BREAKDOWN (sorted by peak multiple desc):")
print("-" * 90)
print(f"{'ID':>4} {'Symbol':<12} {'Call Time':<17} {'Peak':>7}  {'Normal':<6} {'Stoploss':<9} {'Stop Hit'}")
print("-" * 90)
for r in results:
    ns = "WIN" if r["normal_win"] else "loss"
    ss = "WIN" if r["sl_win"] else "loss"
    hit = "yes" if r["sl_hit"] else "-"
    print(f"{r['id']:>4} {r['symbol']:<12} {r['time']:<17} {r['mult']:>6.2f}x  {ns:<6} {ss:<9} {hit}")

print()
print(f"\nSummary — Normal: {n_wins}/{N} ({n_wins/N*100:.1f}%) | Stoploss: {sl_wins}/{N} ({sl_wins/N*100:.1f}%)")