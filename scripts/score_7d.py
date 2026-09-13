"""
Extend scoring to a 7-DAY window using HOURLY candles.

Why: the 12h/1-min scoring may miss pumps that happened AFTER 24h.
Entry stays at the call-minute open (from corrected 1-min data);
peak = max(HIGH) over [call_ts, call_ts + 7 days] from hourly candles.

One API request per call (hourly x 7d = ~168 candles < 1000 limit).
Writes results to a new table `calls_7d` for comparison (does NOT touch calls).

Usage: python scripts/score_7d.py <db> <start_time_label>
"""
import sys
import sqlite3
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import contextmanager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
sys.path.insert(0, str(Path(__file__).parent.parent))

DB_NAME = sys.argv[1] if len(sys.argv) > 1 else "simulation_666.db"
LABEL = sys.argv[2] if len(sys.argv) > 2 else datetime.utcnow().isoformat()

SIM_DB = Path(__file__).parent.parent / DB_NAME
# isolation_level=None = autocommit mode (mirrors db.py); required so the
# manual BEGIN/COMMIT in _transaction_context never collides with an implicit
# transaction that sqlite3 opens on the first DML statement.
conn = sqlite3.connect(SIM_DB, isolation_level=None)
conn.row_factory = sqlite3.Row

@contextmanager
def _transaction_context():
    conn.execute("BEGIN;")
    try:
        yield conn
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise

import db
db.get_connection = lambda: conn
db.transaction = _transaction_context
import pricing.pipeline_v2 as pv2
pv2.get_connection = lambda: conn
pv2.transaction = _transaction_context
import pricing.backtest as bt_mod
bt_mod.get_connection = lambda: conn
bt_mod.transaction = _transaction_context
import pricing.cache as cache_mod
cache_mod.get_connection = lambda: conn
cache_mod.transaction = _transaction_context

from pricing.geckoterminal_v2 import GeckoTerminalClientV2, GeckoTerminalError

# Ensure table exists
conn.execute("""
    CREATE TABLE IF NOT EXISTS calls_7d (
        id INTEGER PRIMARY KEY,
        call_timestamp TEXT,
        entry_price_usd REAL,
        peak_price_usd REAL,
        peak_timestamp TEXT,
        peak_profit_pct REAL,
        is_win INTEGER DEFAULT 0,
        status TEXT,
        note TEXT
    )
""")
# Reset for this run
conn.execute("DELETE FROM calls_7d")
conn.commit()

# Correct pools from token_meta
meta = {
    r["address"]: r["dex_pool_id"]
    for r in conn.execute("SELECT address, dex_pool_id FROM token_meta").fetchall()
}

calls = conn.execute("""
    SELECT id, token_address, token_symbol, call_timestamp, entry_price_usd, pool_address
    FROM calls WHERE status IN ('win','loss') ORDER BY call_timestamp
""").fetchall()

client = GeckoTerminalClientV2()
start_time = time.monotonic()
results = []

print(f"[{LABEL}] Scoring {len(calls)} calls over a 7-day hourly window (1 req/call)...\n")

for idx, call in enumerate(calls, 1):
    token = call["token_address"]
    pool = meta.get(token) or call["pool_address"]
    entry = call["entry_price_usd"]
    call_id = call["id"]
    call_ts_naive = datetime.fromisoformat(call["call_timestamp"].replace("Z", "+00:00")).replace(tzinfo=None)

    if not pool or not entry:
        conn.execute("INSERT OR REPLACE INTO calls_7d (id, call_timestamp, entry_price_usd, status, note) VALUES (?,?,?,'unpriceable_loss','no pool/entry')",
                     (call_id, call["call_timestamp"], entry))
        results.append({"id": call_id, "symbol": call["token_symbol"] or "?", "time": call["call_timestamp"][:16],
                        "peak_mult": None, "status": "unpriceable_loss"})
        continue

    start = call_ts_naive - timedelta(minutes=5)
    end = call_ts_naive + timedelta(days=7)
    try:
        candles = client.fetch_ohlcv(pool, token, start, end, aggregate="hour")
    except GeckoTerminalError as e:
        conn.execute("INSERT OR REPLACE INTO calls_7d (id, call_timestamp, entry_price_usd, status, note) VALUES (?,?,?,'unpriceable_loss',?)",
                     (call_id, call["call_timestamp"], entry, repr(e)[:150]))
        results.append({"id": call_id, "symbol": call["token_symbol"] or "?", "time": call["call_timestamp"][:16],
                        "peak_mult": None, "status": "unpriceable_loss"})
        continue

    # Peak = max HIGH over [call_ts, call_ts+7d]
    peak = max((c.high for c in candles if c.timestamp >= call_ts_naive), default=None)
    peak_ts = max((c for c in candles if c.timestamp >= call_ts_naive), key=lambda c: c.high, default=None)

    if peak is None or peak <= 0:
        conn.execute("INSERT OR REPLACE INTO calls_7d (id, call_timestamp, entry_price_usd, status, note) VALUES (?,?,?,'unpriceable_loss','no candles after call')",
                     (call_id, call["call_timestamp"], entry))
        results.append({"id": call_id, "symbol": call["token_symbol"] or "?", "time": call["call_timestamp"][:16],
                        "peak_mult": None, "status": "unpriceable_loss"})
        continue

    mult = peak / entry
    is_win = 1 if mult >= 2.0 else 0
    peak_ts_str = peak_ts.timestamp.strftime("%Y-%m-%dT%H:%M") if peak_ts else None
    peak_pct = (mult - 1) * 100

    conn.execute("INSERT OR REPLACE INTO calls_7d (id, call_timestamp, entry_price_usd, peak_price_usd, peak_timestamp, peak_profit_pct, is_win, status, note) VALUES (?,?,?,?,?,?,?,?,?)",
                 (call_id, call["call_timestamp"], entry, peak, peak_ts_str, peak_pct, is_win,
                  "win" if is_win else "loss", f"pool={pool[:12]}"))
    results.append({"id": call_id, "symbol": call["token_symbol"] or "?", "time": call["call_timestamp"][:16],
                    "peak_mult": mult, "status": "win" if is_win else "loss", "peak_ts": peak_ts_str})

    if idx % 10 == 0:
        elapsed = time.monotonic() - start_time
        rpm = idx / (elapsed / 60)
        eta = (elapsed / idx) * (len(calls) - idx)
        print(f"  {idx}/{len(calls)} | {rpm:.1f} calls/min | ETA {eta/60:.1f} min")

conn.commit()
elapsed = time.monotonic() - start_time

# Report
print(f"\n{'='*70}")
print(f"7-DAY HOURLY RESULTS — {DB_NAME} (started {LABEL})")
print(f"{'='*70}")
print(f"Finished in {elapsed:.0f}s ({elapsed/60:.1f} min) — {len(calls)} calls\n")

wins = [r for r in results if r["status"] == "win"]
losses = [r for r in results if r["status"] == "loss"]
unp = [r for r in results if r["status"] == "unpriceable_loss"]
decided = len(wins) + len(losses)
wr = len(wins) / decided * 100 if decided else 0
print(f"7-day window: {len(wins)} wins / {len(losses)} losses / {len(unp)} unpriceable")
print(f"Win rate (7d hourly, >=2x): {wr:.1f}%  (vs 12h 1-min: see compare)\n")

# Compare against existing 12h results
print(f"{'ID':>4} {'Sym':<12} {'Call':<17} {'12h':>7} {'7d':>7} {'7d peak TS':<16} Chg")
print("-" * 78)
for r in sorted(results, key=lambda x: x["time"]):
    old = conn.execute("SELECT peak_profit_pct, status FROM calls WHERE id=?", (r["id"],)).fetchone()
    old_mult = (old["peak_profit_pct"] or 0) / 100 + 1 if old and old["peak_profit_pct"] else 1.0
    nm = r["peak_mult"]
    if nm is None:
        print(f"{r['id']:>4} {r['symbol']:<12} {r['time']:<17} {old_mult:>6.2f}x {'N/A':>7} {'':<16} unp")
        continue
    chg = "NEW WIN" if (old and not old["status"] == "win" and r["status"] == "win") else (
        "LOST" if (old and old["status"] == "win" and r["status"] != "win") else "")
    print(f"{r['id']:>4} {r['symbol']:<12} {r['time']:<17} {old_mult:>6.2f}x {nm:>6.2f}x {r.get('peak_ts','') or '':<16} {chg}")

# CSV
csv_path = SIM_DB.parent / f"{Path(DB_NAME).stem}_7d.csv"
with open(csv_path, "w") as f:
    f.write("Call ID,Symbol,Call Time,12h Peak,7d Peak,7d Peak Time,7d Status\n")
    for r in sorted(results, key=lambda x: x["time"]):
        old = conn.execute("SELECT peak_profit_pct, status FROM calls WHERE id=?", (r["id"],)).fetchone()
        old_mult = f"{(old['peak_profit_pct'] or 0)/100+1:.2f}x" if old and old["peak_profit_pct"] else "N/A"
        nm = f"{r['peak_mult']:.2f}x" if r["peak_mult"] else "N/A"
        f.write(f"{r['id']},{r['symbol']},{r['time']},{old_mult},{nm},{r.get('peak_ts','')},{r['status']}\n")
print(f"\nCSV: {csv_path}")