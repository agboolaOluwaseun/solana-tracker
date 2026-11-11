"""Repair BIG (Yeezus #5381 + ch23 #5287) — GT USD-curve frozen on a
TTWO-quoted pumpswap pool (2026-11-11, user: 'big cannot be a loss na').

Root cause: GT's OHLCV/price feed for pool 5Tecgc… (BIG/$TTWO) stopped
updating Nov 9 20:00 (USD conversion of the exotic TTWO quote broke) while
the pool traded on to x8-12. Every rescore pass re-walked the frozen cached
curve and kept the x1.00 'provisional loss'. No existing tripwire caught it:
the seat-listing confirmed BIG IS base (seat correct), and the magnitude
tripwire (ORIENT_RATIO=20) measures flip-scale errors, not a stale x8.
DS live (x8.5) and Birdeye (address-keyed, x9.63 peak) independently agree
the GT curve is the liar.

Fix = the already-blessed flip-victim lane: stamp pool_is_base=0 -> wipe
poisoned GT candles -> reset to pending -> reprice through price_one_call
with allow_birdeye=True (operator initial-scan rights; mirrors the boot-scan
policy). Birdeye series saved to token-primary cache (hour+minute) so the
day-7 finalization and all future passes walk it API-free, demotion-guarded.
"""
import sys, time
sys.path.insert(0, ".")
from datetime import datetime, timezone, timedelta
import sqlite3

from pricing.geckoterminal import GeckoTerminalClient
from pipeline import price_one_call, apply_eval7d, apply_backtest, persist_stoploss_result

conn = sqlite3.connect("kolfi.db", timeout=8)
conn.row_factory = sqlite3.Row

ROWS = [5381, 5287]
MINT = "34DXEU2ttEtBSb1TqGJm7izgvYTTkqKcCoiNWdPDpump"

for cid in ROWS:
    r = conn.execute("SELECT * FROM calls WHERE id=?", (cid,)).fetchone()
    ts = datetime.fromisoformat(r["call_timestamp"].replace("Z", "+00:00")).replace(tzinfo=None)
    meta = conn.execute("SELECT * FROM token_meta WHERE chain='sol' AND address=?", (MINT,)).fetchone()
    if meta:
        conn.execute("UPDATE token_meta SET pool_is_base=0 WHERE chain='sol' AND address=?", (MINT,))
    n = conn.execute("SELECT COUNT(*) c FROM price_cache WHERE token_address=? AND source='geckoterminal'", (MINT,)).fetchone()["c"]
    conn.execute("DELETE FROM price_cache WHERE token_address=? AND source='geckoterminal'", (MINT,))
    conn.execute("""UPDATE calls SET status='pending', pending_reason=NULL,
            is_win=0, score_state='live', note=NULL, peak_multiple=NULL,
            max_multiple=NULL, entry_price_usd=NULL, peak_price_usd=NULL
            WHERE id=?""", (cid,))
    conn.commit()
    client = GeckoTerminalClient()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    live = None if now >= ts + timedelta(days=7) else now
    result, sl, r7 = price_one_call("sol", client, MINT, ts, now=live, allow_birdeye=True)
    eff = apply_eval7d(cid, r7) if r7 is not None else cid
    if r7 is None:
        apply_backtest(r["channel_id"], cid, 0, result)
    persist_stoploss_result(eff, sl)
    st = conn.execute("SELECT status, peak_multiple, note FROM calls WHERE id=?", (eff,)).fetchone()
    print(f"#{cid} -> {st['status']} peak=x{st['peak_multiple']} note={st['note']}")
    time.sleep(2)
