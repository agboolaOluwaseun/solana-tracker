"""
Simulate initial channel scan for a channel using pipeline_v2.
Fetches from April 1st, parses, deduplicates, prices, and generates report.

Usage: python scripts/simulate_channel.py <username> <title> <output_db>
"""
import sys
import os
import sqlite3
import time
import logging
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

sys.path.insert(0, str(Path(__file__).parent.parent))

# Args: username (with @), title, db name
USERNAME = sys.argv[1] if len(sys.argv) > 1 else "@marcellcooks"
TITLE = sys.argv[2] if len(sys.argv) > 2 else "Marcell's Trenches"
SIM_DB_NAME = sys.argv[3] if len(sys.argv) > 3 else "simulation_trenches.db"

SIM_DB = Path(__file__).parent.parent / SIM_DB_NAME
if SIM_DB.exists():
    SIM_DB.unlink()

# autocommit (mirrors db.py); avoids BEGIN-in-transaction on re-runs
conn = sqlite3.connect(SIM_DB, isolation_level=None)
conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
schema_path = Path(__file__).parent.parent / "schema.sql"
conn.executescript(schema_path.read_text())
conn.commit()

cols = {r["name"] for r in conn.execute("PRAGMA table_info(calls)").fetchall()}
if "pending_reason" not in cols:
    conn.execute("ALTER TABLE calls ADD COLUMN pending_reason TEXT")
conn.commit()

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

from config import settings
from ingestion.telethon_fetcher import fetch_window_sync
from ingestion.address_parser import parse_message
from pipeline import deduplicate_calls, ensure_channel, make_buckets, persist_parsed_call

import pricing.pipeline_v2 as pipeline_v2
pipeline_v2.get_connection = lambda: conn
pipeline_v2.transaction = _transaction_context

import pricing.backtest as backtest_mod
backtest_mod.get_connection = lambda: conn
backtest_mod.transaction = _transaction_context

from pricing.pipeline_v2 import price_calls_v2

print("=" * 70)
print(f"SIMULATION: {TITLE}")
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
    USERNAME,
    window_start,
    window_end,
    limit=None,
    progress_cb=on_scan,
)

if isinstance(fetched, tuple):
    messages, channel_title = fetched
else:
    messages = fetched
    channel_title = TITLE

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

all_parsed.sort(key=lambda c: c.timestamp)
all_parsed = deduplicate_calls(all_parsed)
deduped_count = len(all_parsed)
print(f"  After dedup: {deduped_count} calls")
print()

# Ensure channel exists
tg_id = messages[0].channel_id if messages else abs(hash(USERNAME)) % (10 ** 12)
channel_id = ensure_channel(
    USERNAME, tg_id, channel_title,
    USERNAME.lstrip("@"),
    window_start, window_end,
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

# Price calls
print("Step 4: Pricing calls with pipeline_v2...")
pending = conn.execute(
    "SELECT id, message_id, token_address, token_symbol, call_timestamp "
    "FROM calls WHERE channel_id = ? AND status = 'pending' ORDER BY call_timestamp ASC",
    (channel_id,),
).fetchall()

print(f"  Pricing {len(pending)} calls...")
print()

call_dicts = [
    {"id": row["id"], "token_address": row["token_address"], "call_timestamp": row["call_timestamp"]}
    for row in pending
]

t0 = time.monotonic()
results = price_calls_v2(call_dicts)
elapsed = time.monotonic() - t0

print()
print(f"  Pricing complete in {elapsed:.1f}s ({elapsed/60:.1f} min)")
print()

# Update DB
print("Step 5: Updating database with results...")
for call_id, bt_result, sl_result in results:
    conn.execute(
        """UPDATE calls SET
            entry_price_usd=?, peak_price_usd=?, peak_timestamp=?,
            peak_profit_pct=?, is_win=?, status=?, pool_address=?, priced_at=datetime('now')
        WHERE id=?""",
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

# Stats
stats = conn.execute(
    """SELECT COUNT(*) as total,
        SUM(CASE WHEN status='win' THEN 1 ELSE 0 END) as wins,
        SUM(CASE WHEN status='loss' THEN 1 ELSE 0 END) as losses,
        SUM(CASE WHEN status='unpriceable_loss' THEN 1 ELSE 0 END) as unpriceable
    FROM calls""",
).fetchone()

total, wins, losses, unpriceable = stats["total"], stats["wins"], stats["losses"], stats["unpriceable"]
decided = wins + losses
win_rate = wins / decided * 100 if decided else 0

print("=" * 70)
print(f"REPORT: {TITLE}")
print("=" * 70)
print(f"Total calls: {total}")
print(f"Decided: {decided} (wins: {wins}, losses: {losses})")
print(f"Unpriceable: {unpriceable}")
print(f"Win rate (normal strategy): {win_rate:.1f}%")
print()

# Full listing: peak multiples
calls_data = conn.execute(
    """SELECT id, token_symbol, call_timestamp, peak_profit_pct, is_win, status
    FROM calls WHERE status IN ('win','loss') ORDER BY (peak_profit_pct / 100 + 1) DESC""",
).fetchall()

multiples = []
for c in calls_data:
    if c["peak_profit_pct"] is not None:
        multiples.append({
            "id": c["id"],
            "symbol": c["token_symbol"] or "?",
            "time": c["call_timestamp"][:16],
            "mult": c["peak_profit_pct"] / 100 + 1,
            "win": c["is_win"],
        })

print(f"ALL {len(multiples)} CALLS (by peak multiple desc):")
print("-" * 70)
print(f"{'ID':>4} {'Symbol':<14} {'Call Time':<17} {'Peak':>7}  Status")
print("-" * 70)
for m in multiples:
    st = "WIN" if m["win"] else "loss"
    print(f"{m['id']:>4} {m['symbol']:<14} {m['time']:<17} {m['mult']:>6.2f}x  {st}")

csv_path = SIM_DB.parent / f"{Path(SIM_DB_NAME).stem}_report.csv"
with open(csv_path, "w") as f:
    f.write("ID,Symbol,Call Time,Peak Multiple,Status\n")
    for m in multiples:
        st = "WIN" if m["win"] else "LOSS"
        f.write(f"{m['id']},{m['symbol']},{m['time']},{m['mult']:.4f}x,{st}\n")

print()
print(f"CSV exported: {csv_path}")
print("=" * 70)
print("Simulation complete!")
print("=" * 70)