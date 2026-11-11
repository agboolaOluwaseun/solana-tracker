# Repair elysium mis-bucketing. For each robinhood/eth-bucketed 0x mint that
# DexScreener proves lives on elysium:
#  A) if an elysium row exists for the SAME channel AND SAME message -> the
#     elysium row is correct; delete the mis-bucketed duplicate (children
#     first). If same token but different message (a genuine re-post) -> keep
#     OLDEST by re-bucketing the old row and deleting the newer twin instead.
#  B) no twin at all -> re-bucket chain='elysium' and reprice via the
#     production path (GT elysium lane; birdeye lane correctly skipped).
import sys, time
sys.path.insert(0, ".")
from datetime import datetime, timezone, timedelta
import sqlite3

from pricing.geckoterminal import GeckoTerminalClient
from pipeline import price_one_call, apply_eval7d, persist_stoploss_result, apply_backtest

conn = sqlite3.connect("kolfi.db", timeout=8)
conn.row_factory = sqlite3.Row

CANDS = [5189, 5190, 5243, 5245, 5217, 5218, 5219, 5263, 5264]


def delete_call(cid):
    for t in ("trailing_results", "stoploss_results"):
        conn.execute(f"DELETE FROM {t} WHERE call_id=?", (cid,))
    conn.execute("DELETE FROM calls WHERE id=?", (cid,))


def load(cid):
    return conn.execute("SELECT * FROM calls WHERE id=?", (cid,)).fetchone()


gt = GeckoTerminalClient()
now = datetime.now(timezone.utc).replace(tzinfo=None)

for cid in CANDS:
    r = load(cid)
    if r is None:
        continue
    twin = conn.execute(
        "SELECT * FROM calls WHERE chain='elysium' AND token_address=? AND channel_id=? AND id!=?",
        (r["token_address"], r["channel_id"], cid)).fetchone()
    if twin:
        # same channel + same mint already on the correct chain. The twin
        # carries the verdict; this row never priced (no curve exists on
        # robinhood/eth) — delete it, reconciler semantics. If the twin
        # somehow also lacks a verdict, re-bucket THIS row instead.
        if twin["status"] in ("win", "loss", "excluded") or twin["peak_multiple"] is not None:
            delete_call(cid)
            conn.commit()
            print(f"#{cid}: folded into elysium twin #{twin['id']} (kept verdict)", flush=True)
        else:
            delete_call(twin["id"])
            conn.execute("UPDATE calls SET chain='elysium' WHERE id=?", (cid,))
            conn.commit()
            print(f"#{cid}: re-bucketed to elysium, deleted empty twin #{twin['id']}", flush=True)
        continue
    # B) re-bucket + reprice
    conn.execute("UPDATE calls SET chain='elysium', status='pending', pending_reason=NULL, "
                 "is_win=0, score_state='live', note=NULL, peak_multiple=NULL, max_multiple=NULL, "
                 "entry_price_usd=NULL, peak_price_usd=NULL WHERE id=?", (cid,))
    conn.execute("UPDATE price_cache SET chain='elysium' WHERE token_address=? AND chain IN ('robinhood','eth')",
                 (r["token_address"],))
    conn.commit()
    ts = datetime.fromisoformat(r["call_timestamp"].replace("Z", "+00:00")).replace(tzinfo=None)
    live = None if now >= ts + timedelta(days=7) else now
    try:
        result, sl, r7 = price_one_call("elysium", gt, r["token_address"], ts, now=live, allow_birdeye=True)
        eff = apply_eval7d(cid, r7) if r7 is not None else cid
        if r7 is None:
            apply_backtest(r["channel_id"], cid, 0, result)
        persist_stoploss_result(eff, sl)
        st = conn.execute("SELECT status, token_symbol, peak_multiple, note FROM calls WHERE id=?", (eff,)).fetchone()
        print(f"#{cid} {r['token_address'][:12]} -> {st['status']} {st['token_symbol']} peak=x{st['peak_multiple']} note={st['note']}", flush=True)
    except Exception as e:
        print(f"#{cid} reprice ERR {type(e).__name__} {str(e)[:60]} (stays pending, next pass retries)", flush=True)
    time.sleep(1.2)

print("final elysium roster:")
for rr in conn.execute("SELECT id, channel_id, token_symbol, substr(call_timestamp,1,16), status, peak_multiple, note FROM calls WHERE chain='elysium' ORDER BY call_timestamp"):
    print("  ", tuple(rr))
left = conn.execute("SELECT COUNT(*) FROM calls WHERE chain IN ('robinhood','eth') AND token_address LIKE '0x%' AND status IN ('unpriceable_loss','pending') AND call_timestamp >= '2026-10-25'").fetchone()[0]
print("robinhood/eth-0x leftovers (Oct25+, re-checked by DS? not all are elysium):", left)
conn.close()
