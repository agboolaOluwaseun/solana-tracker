"""Snapshot every frontend-visible number from kolfi.db (pre/post diff proof)."""
import json
import sqlite3

c = sqlite3.connect("file:kolfi.db?mode=ro", uri=True)
c.row_factory = sqlite3.Row

out = {"calls": [], "stoploss": [], "trailing": [], "totals": {}}
out["calls"] = [
    dict(r) for r in c.execute("""
        SELECT channel_id, status, COUNT(*) n, SUM(is_win) w
        FROM calls GROUP BY channel_id, status ORDER BY channel_id, status""")]
out["stoploss"] = [
    dict(r) for r in c.execute("""
        SELECT cal.channel_id, sr.status, COUNT(*) n, SUM(sr.is_win) w
        FROM stoploss_results sr JOIN calls cal ON cal.id = sr.call_id
        GROUP BY cal.channel_id, sr.status ORDER BY cal.channel_id, sr.status""")]
out["trailing"] = [
    dict(r) for r in c.execute("""
        SELECT cal.channel_id, tr.status, COUNT(*) n, SUM(tr.is_win) w
        FROM trailing_results tr JOIN calls cal ON cal.id = tr.call_id
        GROUP BY cal.channel_id, tr.status ORDER BY cal.channel_id, tr.status""")]
for t in ("calls", "stoploss_results", "trailing_results", "price_cache",
          "token_meta", "channels"):
    out["totals"][t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
# candle coverage per consumed pool (migration must preserve this exactly)
out["coverage"] = [
    dict(r) for r in c.execute("""
        SELECT cal.token_address t, pc.aggregate agg, COUNT(*) n
        FROM price_cache pc JOIN calls cal
          ON cal.token_address = pc.token_address
         AND (pc.pool_address = cal.pool_address OR pc.pool_address = 'birdeye')
        GROUP BY cal.token_address, pc.aggregate
        ORDER BY cal.token_address""")]

import sys
json.dump(out, open(sys.argv[1], "w"), indent=0)
print("wrote", sys.argv[1], "| rows:", len(out["calls"]), len(out["stoploss"]),
      len(out["trailing"]), len(out["coverage"]), "| totals:", out["totals"])
