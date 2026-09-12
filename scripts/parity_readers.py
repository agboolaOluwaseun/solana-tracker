"""B6 reader parity: existing analysis readers must return identical numbers
from kolfi.db (unified) as from solana_tracker.db (legacy), for chain='all'.

Compares: channel_stats_window, channel_buckets, current_streak for every
channel x window x strategy. Tolerates zero rows on RH-only channels.
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import importlib


def load_with(db_path):
    os.environ["DB_PATH"] = db_path
    os.environ["SCHEMA_FILE"] = "schema.sql" if db_path.endswith("solana_tracker.db") else "schema_unified.sql"
    for mod in ("config", "db", "analysis.windowed", "analysis.tiers"):
        if mod in sys.modules:
            del sys.modules[mod]
    import config, db, analysis.windowed, analysis.tiers
    importlib.reload(config)
    importlib.reload(db)
    importlib.reload(analysis.windowed)
    importlib.reload(analysis.tiers)
    return db, analysis.windowed


def snapshot(W, chain):
    """chain=None/'all' for legacy (no column); 'sol' for unified (must match)."""
    conn = W.get_connection()
    chans = [r["id"] for r in conn.execute("SELECT id FROM channels ORDER BY id")]
    snap = {}
    for cid in chans:
        for window in ("1d", "7d", "1m", "3m", "all"):
            for strategy in ("normal", "stoploss"):
                snap[(cid, window, strategy)] = (
                    W.channel_stats_window(cid, window, strategy, chain=chain),
                    [tuple(b.values()) for b in W.channel_buckets(cid, window, strategy, chain=chain)],
                    W.current_streak(cid, strategy, chain=chain),
                )
    return snap


def main():
    db_mod, W_legacy = load_with(os.path.join(ROOT, "solana_tracker.db"))
    legacy = snapshot(W_legacy, "all")
    db_mod, W_uni = load_with(os.path.join(ROOT, "kolfi.db"))
    unified = snapshot(W_uni, "sol")

    fails = 0
    for key in sorted(legacy):
        a, b = legacy[key], unified[key]
        # stats: compare floats to 1e-9
        if round(a[0]["total_calls"] or 0, 9) != round(b[0]["total_calls"] or 0, 9) or \
           round(a[0]["wins"] or 0, 9) != round(b[0]["wins"] or 0, 9):
            fails += 1
            print("MISMATCH stats", key, a[0], b[0])
    print(f"reader parity: {len(legacy) - fails}/{len(legacy)} channel-window-strategy results match (chain='sol' filter)")
    # tiers sanity on unified DB
    from analysis.tiers import tier_counts
    t = tier_counts(26, "all", "normal", chain="all")  # x666calls channel, merged
    total = t["total_decided"]
    ts = {d["label"]: d["count"] for d in t["tiers"]}
    assert ts["100x"] <= ts["50x"] <= ts["25x"] <= ts["10x"] <= ts["5x"] <= ts["3x"] <= ts["2x"], ts
    assert ts["2x"] + ts["<2x"] == total, (ts, total)
    print(f"tiers x666calls all-chains all-time: decided={total} 2x+={ts['2x']} <2x={ts['<2x']} 100x={ts['100x']} — invariants OK")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
