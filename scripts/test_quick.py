"""
Quick test: price just 5 calls from the simulation database
"""
import sys
import sqlite3
import time
import logging
from pathlib import Path
from contextlib import contextmanager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

sys.path.insert(0, str(Path(__file__).parent.parent))

# Use the existing simulation database
SIM_DB = Path(__file__).parent.parent / "simulation_666.db"
conn = sqlite3.connect(SIM_DB)
conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))

@contextmanager
def _transaction_context():
    conn.execute("BEGIN;")
    try:
        yield conn
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise

# Patch db module
import db
db.get_connection = lambda: conn
db.transaction = _transaction_context

# Import and patch pipeline_v2
import pricing.pipeline_v2 as pipeline_v2
pipeline_v2.get_connection = lambda: conn
pipeline_v2.transaction = _transaction_context

import pricing.backtest as backtest_mod
backtest_mod.get_connection = lambda: conn
backtest_mod.transaction = _transaction_context

from pricing.pipeline_v2 import price_calls_v2

print("=" * 70)
print("QUICK TEST: Pricing 5 calls from simulation_666.db")
print("=" * 70)

# Get 5 pending calls
pending = conn.execute(
    "SELECT id, message_id, token_address, token_symbol, raw_text, call_timestamp "
    "FROM calls WHERE channel_id = 1 AND status = 'pending' "
    "ORDER BY call_timestamp ASC LIMIT 5"
).fetchall()

print(f"Found {len(pending)} pending calls")
print()

call_dicts = [
    {
        "id": row["id"],
        "token_address": row["token_address"],
        "call_timestamp": row["call_timestamp"],
    }
    for row in pending
]

print("Starting pricing...")
t0 = time.monotonic()

try:
    results = price_calls_v2(call_dicts)
    elapsed = time.monotonic() - t0
    
    print()
    print(f"Pricing complete in {elapsed:.1f}s")
    print(f"Results: {len(results)} calls priced")
    
    for call_id, bt_result, sl_result in results:
        status = bt_result.status
        peak = bt_result.peak_profit_pct
        print(f"  Call {call_id}: {status}, peak={peak:.2f}%")
        
except Exception as e:
    elapsed = time.monotonic() - t0
    print()
    print(f"ERROR after {elapsed:.1f}s: {e}")
    import traceback
    traceback.print_exc()

print()
print("=" * 70)
