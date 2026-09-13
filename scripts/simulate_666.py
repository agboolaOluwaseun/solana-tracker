"""
Simulate initial channel scan for 666 🔥 Calls using pipeline_v2.
Starts fresh - no existing data, fetches from April 1st.
"""
import sys
import os
import sqlite3
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Create simulation database BEFORE any imports
SIM_DB = Path(__file__).parent.parent / "simulation_666.db"
if SIM_DB.exists():
    SIM_DB.unlink()

# Initialize schema
conn = sqlite3.connect(SIM_DB)
conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
schema_path = Path(__file__).parent.parent / "schema.sql"
schema = schema_path.read_text()
conn.executescript(schema)
conn.commit()

# Add pending_reason column if missing
cols = {r["name"] for r in conn.execute("PRAGMA table_info(calls)").fetchall()}
if "pending_reason" not in cols:
    conn.execute("ALTER TABLE calls ADD COLUMN pending_reason TEXT")
conn.commit()

# Create transaction context manager
@contextmanager
def _transaction_context():
    conn.execute("BEGIN;")
    try:
        yield conn
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise

# Patch db module BEFORE importing anything that uses it
import db
db.get_connection = lambda: conn
db.transaction = _transaction_context

# Now import everything else - they will use our patched db
from config import settings
from ingestion.telethon_fetcher import fetch_window_sync
from ingestion.address_parser import parse_message
from pipeline import deduplicate_calls, ensure_channel, make_buckets, persist_parsed_call

# Import pipeline_v2 and patch its db references directly
import pricing.pipeline_v2 as pipeline_v2
pipeline_v2.get_connection = lambda: conn
pipeline_v2.transaction = _transaction_context

# Also patch backtest module which pipeline_v2 calls
import pricing.backtest as backtest_mod
backtest_mod.get_connection = lambda: conn
backtest_mod.transaction = _transaction_context

from pricing.pipeline_v2 import price_calls_v2

print("=" * 70)
print("SIMULATION: 666 🔥 Calls Channel - Initial Scan")
print("=" * 70)
print(f"Database: {SIM_DB}")
print(f"Window: April 1, 2026 → now")
print()

# Fetch messages
print("Step 1: Fetching messages from Telegram...")
window_start = datetime(2026, 4, 1, 0, 0, 0)
window_end = datetime.now(timezone.utc).replace(tzinfo=None)

def on_scan(n):
    if n % 100 == 0:
        print(f"  Scanned {n} messages...")

fetched = fetch_window_sync(
    "@x666calls",
    window_start,
    window_end,
    limit=None,
    progress_cb=on_scan,
)

if isinstance(fetched, tuple):
    messages, channel_title = fetched
else:
    messages = fetched
    channel_title = "666 🔥 Calls"

print(f"  Fetched {len(messages)} messages")
print()

# Parse and deduplicate
print("Step 2: Parsing addresses and deduplicating...")
all_parsed = []
for msg in messages:
    parsed = parse_message(msg)
    if parsed is not None:
        all_parsed.append(parsed)

raw_count = len(all_parsed)
print(f"  Raw calls found: {raw_count}")

# Sort chronologically
all_parsed.sort(key=lambda c: c.timestamp)

# Deduplicate
all_parsed = deduplicate_calls(all_parsed)
deduped_count = len(all_parsed)
print(f"  After dedup: {deduped_count} calls")
print()

# Ensure channel exists
tg_id = messages[0].channel_id if messages else abs(hash("@x666calls")) % (10 ** 12)
channel_id = ensure_channel(
    "@x666calls",
    tg_id,
    channel_title,
    "x666calls",
    window_start,
    window_end,
)

# Persist calls
print("Step 3: Persisting calls to database...")
buckets = make_buckets(window_start)
for parsed in all_parsed:
    week_index = buckets.week_index(parsed.timestamp)
    if week_index == 0:
        week_index = max(1, min(settings.window_weeks, week_index or 1))
    persist_parsed_call(channel_id, parsed, week_index)

conn.commit()
print(f"  Persisted {deduped_count} calls")
print()

# Price calls using pipeline_v2
print("Step 4: Pricing calls with pipeline_v2...")
pending = conn.execute(
    "SELECT id, message_id, token_address, token_symbol, raw_text, call_timestamp "
    "FROM calls WHERE channel_id = ? AND status = 'pending' ORDER BY call_timestamp ASC",
    (channel_id,),
).fetchall()

print(f"  Pricing {len(pending)} calls...")
print()

# Prepare call dicts for pipeline_v2
call_dicts = [
    {
        "id": row["id"],
        "token_address": row["token_address"],
        "call_timestamp": row["call_timestamp"],
    }
    for row in pending
]

# Price with pipeline_v2
t0 = time.monotonic()
results = price_calls_v2(call_dicts)
elapsed = time.monotonic() - t0

print()
print(f"  Pricing complete in {elapsed:.1f}s ({elapsed/60:.1f} min)")
print()

# Update database with results
print("Step 5: Updating database with pricing results...")
for call_id, bt_result, sl_result in results:
    conn.execute(
        """
        UPDATE calls SET
            entry_price_usd = ?,
            peak_price_usd = ?,
            peak_timestamp = ?,
            peak_profit_pct = ?,
            is_win = ?,
            status = ?,
            pool_address = ?,
            priced_at = datetime('now')
        WHERE id = ?
        """,
        (
            bt_result.entry_price_usd,
            bt_result.peak_price_usd,
            bt_result.peak_timestamp.isoformat() if bt_result.peak_timestamp else None,
            bt_result.peak_profit_pct,
            1 if bt_result.is_win else 0,
            bt_result.status,
            bt_result.pool_address,
            call_id,
        ),
    )
conn.commit()
print("  Database updated")
print()

# Generate report
print("=" * 70)
print("REPORT: 666 🔥 Calls Channel Simulation")
print("=" * 70)
print()

# Overall stats
total_calls = conn.execute(
    "SELECT COUNT(*) as cnt FROM calls WHERE channel_id = ?", (channel_id,)
).fetchone()["cnt"]

wins = conn.execute(
    "SELECT COUNT(*) as cnt FROM calls WHERE channel_id = ? AND is_win = 1", (channel_id,)
).fetchone()["cnt"]

losses = conn.execute(
    "SELECT COUNT(*) as cnt FROM calls WHERE channel_id = ? AND status = 'loss'", (channel_id,)
).fetchone()["cnt"]

unpriceable = conn.execute(
    "SELECT COUNT(*) as cnt FROM calls WHERE channel_id = ? AND status = 'unpriceable_loss'", (channel_id,)
).fetchone()["cnt"]

decided = wins + losses
win_rate = (wins / decided * 100) if decided > 0 else 0

print(f"Total calls: {total_calls}")
print(f"Decided: {decided} (wins: {wins}, losses: {losses})")
print(f"Unpriceable: {unpriceable}")
print(f"Win rate (normal strategy): {win_rate:.1f}%")
print()

# Stoploss strategy analysis
print("Stoploss Strategy Analysis (50% stoploss):")
print("-" * 70)

# For this simulation, we'll analyze the peak_profit_pct distribution
# to estimate stoploss performance

calls_data = conn.execute(
    """
    SELECT id, token_symbol, call_timestamp, entry_price_usd, peak_price_usd,
           peak_profit_pct, is_win, status
    FROM calls
    WHERE channel_id = ? AND status IN ('win', 'loss')
    ORDER BY call_timestamp ASC
    """,
    (channel_id,),
).fetchall()

# Analyze peak multiples
multiples = []
for call in calls_data:
    if call["peak_profit_pct"] is not None:
        mult = call["peak_profit_pct"] / 100 + 1
        multiples.append({
            "id": call["id"],
            "symbol": call["token_symbol"] or "?",
            "call_time": call["call_timestamp"][:16],
            "peak_mult": mult,
            "is_win": call["is_win"],
        })

# Sort by peak multiple descending
multiples.sort(key=lambda x: x["peak_mult"], reverse=True)

# Count stoploss wins (peak >= 2x before hitting -50%)
stoploss_wins = sum(1 for m in multiples if m["peak_mult"] >= 2.0)
stoploss_losses = len(multiples) - stoploss_wins
stoploss_win_rate = (stoploss_wins / len(multiples) * 100) if multiples else 0

print(f"Stoploss wins (peak >= 2x): {stoploss_wins}")
print(f"Stoploss losses (peak < 2x): {stoploss_losses}")
print(f"Stoploss win rate: {stoploss_win_rate:.1f}%")
print()

# Top 20 calls by peak multiple
print("Top 20 Calls by Peak Multiple:")
print("-" * 70)
print(f"{'ID':>6} {'Symbol':<12} {'Call Time':<18} {'Peak':>8} {'Status':<8}")
print("-" * 70)
for m in multiples[:20]:
    status = "WIN" if m["is_win"] else "LOSS"
    print(f"{m['id']:>6} {m['symbol']:<12} {m['call_time']:<18} {m['peak_mult']:>7.2f}x {status:<8}")
print()

# Bottom 20 calls by peak multiple
print("Bottom 20 Calls by Peak Multiple:")
print("-" * 70)
print(f"{'ID':>6} {'Symbol':<12} {'Call Time':<18} {'Peak':>8} {'Status':<8}")
print("-" * 70)
for m in multiples[-20:]:
    status = "WIN" if m["is_win"] else "LOSS"
    print(f"{m['id']:>6} {m['symbol']:<12} {m['call_time']:<18} {m['peak_mult']:>7.2f}x {status:<8}")
print()

# Export to CSV for Google Sheets
csv_path = SIM_DB.parent / "simulation_666_report.csv"
with open(csv_path, "w") as f:
    f.write("ID,Symbol,Call Time,Peak Multiple,Status\n")
    for m in multiples:
        status = "WIN" if m["is_win"] else "LOSS"
        f.write(f"{m['id']},{m['symbol']},{m['call_time']},{m['peak_mult']:.4f}x,{status}\n")

print(f"Full report exported to: {csv_path}")
print()
print("=" * 70)
print("Simulation complete!")
print("=" * 70)
