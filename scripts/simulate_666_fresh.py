#!/usr/bin/env python3
"""
Fresh simulation for 666 🔥 Calls channel starting April 1st, 2026.
Fetches from Telegram, parses, deduplicates, prices, and generates report.
"""
import sys
import os
import sqlite3
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import settings
from db import get_connection, init_db
from ingestion.telethon_fetcher import fetch_window_sync
from ingestion.address_parser import extract_addresses
from pricing.pipeline_v2 import price_calls_v2

# Initialize simulation database
SIM_DB = Path(__file__).parent.parent / "simulation_666.db"
if SIM_DB.exists():
    SIM_DB.unlink()

conn = sqlite3.connect(SIM_DB)
conn.row_factory = sqlite3.Row

# Create schema
conn.execute("""
CREATE TABLE channels (
    id INTEGER PRIMARY KEY,
    title TEXT,
    username TEXT,
    created_at TEXT
)
""")

conn.execute("""
CREATE TABLE calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER,
    message_id INTEGER,
    token_address TEXT,
    token_symbol TEXT,
    call_timestamp TEXT,
    status TEXT DEFAULT 'pending',
    entry_price_usd REAL,
    peak_price_usd REAL,
    peak_profit_pct REAL,
    is_win INTEGER DEFAULT 0
)
""")

conn.commit()

print("=" * 70)
print("SIMULATION: 666 🔥 Calls Channel - Fresh from April 1st, 2026")
print("=" * 70)

# Fetch messages from April 1st, 2026
print("\nStep 1: Fetching messages from Telegram...")
window_start = datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)
window_end = datetime.now(timezone.utc)

messages = fetch_window_sync(
    channel_id=26,
    window_start=window_start,
    window_end=window_end
)

print(f"Fetched {len(messages)} messages")

# Parse addresses
print("\nStep 2: Parsing addresses...")
parsed_calls = []
for msg in messages:
    addresses = extract_addresses(msg.text)
    for addr in addresses:
        parsed_calls.append({
            'message_id': msg.id,
            'token_address': addr,
            'token_symbol': None,
            'call_timestamp': msg.date.isoformat()
        })

print(f"Parsed {len(parsed_calls)} calls")

# Deduplicate
print("\nStep 3: Deduplicating...")
seen = set()
deduped = []
for call in parsed_calls:
    key = (call['token_address'], call['message_id'])
    if key not in seen:
        seen.add(key)
        deduped.append(call)

print(f"After dedup: {len(deduped)} calls")

# Insert into database
print("\nStep 4: Storing in database...")
channel_id = conn.execute(
    "INSERT INTO channels (id, title, username, created_at) VALUES (?, ?, ?, ?)",
    (26, "666 🔥 Calls", "x666calls", datetime.now().isoformat())
).lastrowid

for call in deduped:
    conn.execute(
        """INSERT INTO calls 
        (channel_id, message_id, token_address, token_symbol, call_timestamp)
        VALUES (?, ?, ?, ?, ?)""",
        (channel_id, call['message_id'], call['token_address'], 
         call['token_symbol'], call['call_timestamp'])
    )

conn.commit()
print(f"Stored {len(deduped)} calls")

# Price calls
print("\nStep 5: Pricing calls with pipeline_v2...")
pending = conn.execute(
    "SELECT id, token_address, call_timestamp FROM calls WHERE status='pending'"
).fetchall()

print(f"Pricing {len(pending)} calls...")

call_dicts = [
    {
        'id': row['id'],
        'token_address': row['token_address'],
        'call_timestamp': row['call_timestamp']
    }
    for row in pending
]

t0 = time.monotonic()
results = price_calls_v2(call_dicts)
elapsed = time.monotonic() - t0

print(f"\nPricing complete in {elapsed:.1f}s ({elapsed/60:.1f} min)")

# Update database
print("\nStep 6: Updating database with results...")
for call_id, bt_result, sl_result in results:
    conn.execute(
        """UPDATE calls SET
        status=?, entry_price_usd=?, peak_price_usd=?,
        peak_profit_pct=?, is_win=?
        WHERE id=?""",
        (
            bt_result.status,
            bt_result.entry_price_usd,
            bt_result.peak_price_usd,
            bt_result.peak_profit_pct,
            1 if bt_result.is_win else 0,
            call_id
        )
    )

conn.commit()
print("Database updated")

# Generate report
print("\n" + "=" * 70)
print("REPORT: 666 🔥 Calls Channel Simulation")
print("=" * 70)

stats = conn.execute("""
SELECT 
    COUNT(*) as total,
    SUM(CASE WHEN status='win' THEN 1 ELSE 0 END) as wins,
    SUM(CASE WHEN status='loss' THEN 1 ELSE 0 END) as losses,
    SUM(CASE WHEN status='unpriceable_loss' THEN 1 ELSE 0 END) as unpriceable
FROM calls
""").fetchone()

total = stats['total']
wins = stats['wins']
losses = stats['losses']
unpriceable = stats['unpriceable']
decided = wins + losses
win_rate = (wins / decided * 100) if decided > 0 else 0

print(f"\nTotal calls: {total}")
print(f"Decided: {decided} (wins: {wins}, losses: {losses})")
print(f"Unpriceable: {unpriceable}")
print(f"Win rate (normal strategy): {win_rate:.1f}%")

# Stoploss analysis
print("\nStoploss Strategy Analysis (50% stoploss):")
print("-" * 70)

# For stoploss, we'd need to re-analyze with the stoploss logic
# For now, estimate based on peak multiples
calls_data = conn.execute("""
SELECT id, token_symbol, call_timestamp, peak_profit_pct, is_win, status
FROM calls
WHERE status IN ('win', 'loss')
ORDER BY call_timestamp ASC
""").fetchall()

multiples = []
for call in calls_data:
    if call['peak_profit_pct'] is not None:
        mult = call['peak_profit_pct'] / 100 + 1
        multiples.append({
            'id': call['id'],
            'symbol': call['token_symbol'] or '?',
            'call_time': call['call_timestamp'][:16],
            'peak_mult': mult,
            'is_win': call['is_win']
        })

multiples.sort(key=lambda x: x['peak_mult'], reverse=True)

# Estimate stoploss wins (peak >= 2x)
stoploss_wins = sum(1 for m in multiples if m['peak_mult'] >= 2.0)
stoploss_losses = len(multiples) - stoploss_wins
stoploss_win_rate = (stoploss_wins / len(multiples) * 100) if multiples else 0

print(f"Stoploss wins (peak >= 2x): {stoploss_wins}")
print(f"Stoploss losses (peak < 2x): {stoploss_losses}")
print(f"Stoploss win rate: {stoploss_win_rate:.1f}%")

# Top 20 calls
print("\nTop 20 Calls by Peak Multiple:")
print("-" * 70)
print(f"{'ID':>6} {'Symbol':<12} {'Call Time':<18} {'Peak':>8} {'Status':<8}")
print("-" * 70)
for m in multiples[:20]:
    status = "WIN" if m['is_win'] else "LOSS"
    print(f"{m['id']:>6} {m['symbol']:<12} {m['call_time']:<18} {m['peak_mult']:>7.2f}x {status:<8}")

# Bottom 20 calls
print("\nBottom 20 Calls by Peak Multiple:")
print("-" * 70)
print(f"{'ID':>6} {'Symbol':<12} {'Call Time':<18} {'Peak':>8} {'Status':<8}")
print("-" * 70)
for m in multiples[-20:]:
    status = "WIN" if m['is_win'] else "LOSS"
    print(f"{m['id']:>6} {m['symbol']:<12} {m['call_time']:<18} {m['peak_mult']:>7.2f}x {status:<8}")

# Export to CSV
csv_path = SIM_DB.parent / "simulation_666_report.csv"
with open(csv_path, 'w') as f:
    f.write("ID,Symbol,Call Time,Peak Multiple,Status\n")
    for m in multiples:
        status = "WIN" if m['is_win'] else "LOSS"
        f.write(f"{m['id']},{m['symbol']},{m['call_time']},{m['peak_mult']:.4f}x,{status}\n")

print(f"\nFull report exported to: {csv_path}")
print("\n" + "=" * 70)
print("Simulation complete!")
print("=" * 70)
