"""
Reprice unpriceable/failed calls in a simulation DB using pipeline_v2 client.
Usage: python scripts/reprice_failures_db.py <db_name>
"""
import sys
import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
sys.path.insert(0, str(Path(__file__).parent.parent))

DB_NAME = sys.argv[1] if len(sys.argv) > 1 else "simulation_trenches.db"
SIM_DB = Path(__file__).parent.parent / DB_NAME
# autocommit (mirrors db.py); avoids BEGIN-in-transaction on re-runs
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

import pricing.pipeline_v2 as pipeline_v2
pipeline_v2.get_connection = lambda: conn
pipeline_v2.transaction = _transaction_context

import pricing.backtest as backtest_mod
backtest_mod.get_connection = lambda: conn
backtest_mod.transaction = _transaction_context

from pricing.pipeline_v2 import resolve_pool_batch_sequential
from pricing.geckoterminal_v2 import GeckoTerminalClientV2
from pricing.backtest import backtest_call

targets = conn.execute("""
    SELECT id, token_address, token_symbol, call_timestamp, status FROM calls
    WHERE status IN ('unpriceable_loss', 'pending')
""").fetchall()

print(f"Repricing {len(targets)} calls...\n")
client = GeckoTerminalClientV2()

# Resolve the ones not already in token_meta
addrs = list({r["token_address"] for r in targets})
resolved = conn.execute("SELECT address, dex_pool_id FROM token_meta").fetchall()
known = {r["address"] for r in resolved}
to_resolve = [a for a in addrs if a not in known]
if to_resolve:
    pools = resolve_pool_batch_sequential(to_resolve, client)
    print(f"Pool resolution: {len(pools)}/{len(to_resolve)} newly resolved\n")
else:
    pools = {}
    print("All targets already have pools in token_meta — skipping resolution\n")

for row in targets:
    call_id = row["id"]
    addr = row["token_address"]
    ts_str = row["call_timestamp"]
    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).replace(tzinfo=None)

    try:
        bt, sl = backtest_call(client, addr, ts)
        mult = (bt.peak_profit_pct or 0) / 100 + 1 if bt.peak_profit_pct else None
        mult_s = f"{mult:.2f}x" if mult else "N/A"
        entry = f"${bt.entry_price_usd:.8g}" if bt.entry_price_usd else "N/A"
        print(f"Call {call_id} ({ts_str[:16]}): {bt.status} | peak {mult_s} | entry {entry}")

        conn.execute(
            """UPDATE calls SET
                status=?, entry_price_usd=?, peak_price_usd=?, peak_timestamp=?,
                peak_profit_pct=?, is_win=?, pool_address=?, priced_at=datetime('now')
            WHERE id=?""",
            (
                bt.status, bt.entry_price_usd, bt.peak_price_usd,
                bt.peak_timestamp.isoformat() if bt.peak_timestamp else None,
                bt.peak_profit_pct, 1 if bt.is_win else 0,
                bt.pool_address, call_id,
            ),
        )
    except Exception as e:
        print(f"Call {call_id}: ERROR {repr(e)[:120]}")

conn.commit()
print("\nDone.")