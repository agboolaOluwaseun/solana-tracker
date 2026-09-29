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
# coverage invariant: DISTINCT candle timestamps per (token, aggregate) over
# the consumed set (pool key pre-migration; everything post-migration is
# consumed by construction). Dedup may drop same-ts duplicate rows but must
# never drop a timestamp.
consumed_pairs = c.execute("""
    SELECT DISTINCT token_address t, pool_address p FROM calls
    WHERE token_address IS NOT NULL AND token_address<>''
      AND pool_address NOT IN ('', 'unknown')""").fetchall()
birdeye_tokens = {r["t"] for r in c.execute("""
    SELECT token_address t FROM calls
    WHERE COALESCE(note,'')||COALESCE(engine,'') LIKE '%birdeye%'""")}
import collections
cov = collections.defaultdict(set)
for g in c.execute("SELECT token_address t, pool_address p, aggregate agg, candle_ts ts FROM price_cache"):
    ok = (g["t"], g["p"]) in {(r["t"], r["p"]) for r in consumed_pairs}
    ok = ok or (g["p"] == "birdeye" and g["t"] in birdeye_tokens)
    if ok:
        cov[(g["t"], g["agg"])].add(g["ts"])
out["coverage"] = {f"{k[0]}|{k[1]}": sorted(v) for k, v in cov.items()}

import sys
json.dump(out, open(sys.argv[1], "w"), indent=0)
print("wrote", sys.argv[1], "| rows:", len(out["calls"]), len(out["stoploss"]),
      len(out["trailing"]), len(out["coverage"]), "| totals:", out["totals"])
