"""
Reprice the network-failed tokens from the 666 simulation.
Calls 43 + 77 failed pool resolution during the DNS outage; call 88 is pending.
Uses pipeline_v2 client directly (GeckoTerminal + DexScreener) to resolve + price.
"""
import sys
import sqlite3
import logging
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

sys.path.insert(0, str(Path(__file__).parent.parent))

SIM_DB = Path(__file__).parent.parent / "simulation_666.db"
conn = sqlite3.connect(SIM_DB)
conn.row_factory = sqlite3.Row

from pricing.pipeline_v2 import resolve_pool_batch_sequential
from pricing.geckoterminal_v2 import GeckoTerminalClientV2
from pricing.backtest import backtest_call

targets = conn.execute("""
    SELECT id, token_address, call_timestamp, status FROM calls
    WHERE status IN ('unpriceable_loss', 'pending')
""").fetchall()

print(f"Repricing {len(targets)} calls...\n")

client = GeckoTerminalClientV2()

# Phase 1: resolve these specific tokens
addrs = list({r["token_address"] for r in targets})
pools = resolve_pool_batch_sequential(addrs, client)
print(f"Pool resolution: {len(pools)}/{len(addrs)} resolved\n")

for row in targets:
    call_id = row["id"]
    addr = row["token_address"]
    ts_str = row["call_timestamp"]
    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).replace(tzinfo=None)

    if addr not in pools:
        print(f"Call {call_id}: STILL unresolvable — pool not found")
        conn.execute(
            "UPDATE calls SET status='unpriceable_loss', priced_at=datetime('now') WHERE id=?",
            (call_id,),
        )
        continue

    try:
        bt, sl = backtest_call(client, addr, ts)
        mult = (bt.peak_profit_pct or 0) / 100 + 1 if bt.peak_profit_pct else None
        mult_s = f"{mult:.2f}x" if mult else "N/A"
        print(f"Call {call_id} ({ts_str[:16]}): {bt.status} | peak {mult_s} | entry ${bt.entry_price_usd or 0:.8g}")

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
        print(f"Call {call_id}: ERROR {e}")
        conn.execute(
            "UPDATE calls SET status='unpriceable_loss', priced_at=datetime('now') WHERE id=?",
            (call_id,),
        )

conn.commit()
print("\nDone.")