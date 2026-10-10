"""Flip-victim repair — 14 suspects confirmed by the GT seat listing + the
Birdeye probe (user 2026-10-08: 'investigate those 14 suspect rows').

Evidence: stored entry vs Birdeye entry ratio > 20x on 14/15 — the stored
verdict was computed on the OTHER asset's curve (tOpenAI on Smiski $1,638,
PHISH, TREE, BAG, FUD, Quant, CLAUDE, SXSI, HABIBINU, BOT, XSI, PLAGUE-w
(2nd, different mint — flip proven by price), SDOG, backpack).
ZINC: entries agree 1.003x — legit $2.8 token, EXCLUDED (never touches it).

Method (production path only):
 1. stamp pool_is_base=0 (the seat verdict GT gave us; SDOG/SXSI got the
    listing's own answer; the others' probe was Birdeye-vs-stored — the
    magnitude IS the flip proof the post-score tripwire already formalizes)
 2. clear contaminated GT cache rows (stamped with the flipped pool)
 3. wipe verdict -> pending, reset score_state
 4. reprice through price_one_call(allow_birdeye=True) — the guard refuses
    the flipped curve, the Birdeye series (saved by the probe) or a fresh
    fetch decides honestly; apply_eval7d + persist_stoploss_result persist.
 5. final: every repaired row must show its true verdict.
"""
import sys
sys.path.insert(0, ".")
import json
import sqlite3
from datetime import datetime, timezone

import pipeline
from pipeline import price_one_call, apply_eval7d, persist_stoploss_result
from pricing.geckoterminal import GeckoTerminalClient

EVID = json.load(open("/tmp/suspects_birdeye.json"))
VICTIMS = [e for e in EVID if e.get("flag") == "FLIP CONFIRMED"]
print(f"victims: {len(VICTIMS)} (of {len(EVID)} suspects)")

conn = pipeline.get_connection()
gt = GeckoTerminalClient()
now = datetime.now(timezone.utc).replace(tzinfo=None)
results = []

for e in VICTIMS:
    cid = e["id"]
    row = conn.execute("""SELECT id, token_address, call_timestamp, status,
        score_state, pool_address FROM calls WHERE id=?""", (cid,)).fetchone()
    mint = row["token_address"]
    ts = datetime.fromisoformat(str(row["call_timestamp"]).replace("Z", ""))
    print(f"\n== #{cid} {e['sym']} ({row['status']}/{row['score_state']}) mint {mint[:12]}… ==")

    # 1-3. seat stamp + clean contaminated GT cache + verdict wipe
    with pipeline.transaction() as tc:
        tc.execute("""UPDATE token_meta SET pool_is_base=0 WHERE chain='sol'
            AND address=?""", (mint,))
        if tc.execute("SELECT 1 FROM token_meta WHERE chain='sol' AND address=?",
                      (mint,)).fetchone() is None:
            tc.execute("""INSERT INTO token_meta (chain, address, symbol,
                pool_is_base, first_seen) VALUES ('sol',?,?,0,datetime('now'))""",
                (mint, e["sym"]))
        deleted = tc.execute("""DELETE FROM price_cache WHERE chain='sol'
            AND token_address=? AND source='geckoterminal'""", (mint,)).rowcount
        tc.execute("""UPDATE calls SET status='pending', pending_reason=NULL,
            is_win=0, score_state='live', note=NULL, peak_multiple=NULL,
            entry_price_usd=NULL, peak_price_usd=NULL
            WHERE id=?""", (cid,))
    print(f"   stamped seat=0, cleared {deleted} GT cache rows, reset pending")

    # 4. reprice via production path (rescue allowed: operator initial-scan
    #    class; flipped curve refuses, saved birdeye series decides)
    live = None if now >= ts + __import__("datetime").timedelta(days=7) else now
    res, sl, r7 = price_one_call("sol", gt, mint, ts, now=live, allow_birdeye=True)
    if r7 is None:
        print("   !! legacy result — leaving pending for next pass")
        continue
    cid = apply_eval7d(cid, r7)
    persist_stoploss_result(cid, sl)
    fin = conn.execute("""SELECT status, score_state, round(peak_multiple,2) pk,
        round(entry_price_usd,10) ent, substr(COALESCE(note,''),1,36) note,
        (SELECT COUNT(*) FROM stoploss_results s WHERE s.call_id=c.id) sl,
        (SELECT COUNT(*) FROM trailing_results t WHERE t.call_id=c.id) tr
        FROM calls c WHERE id=?""", (cid,)).fetchone()
    print(f"   -> {fin['status']} state={fin['score_state']} peak={fin['pk']}x "
          f"entry={fin['ent']} note={fin['note']!r} sl={fin['sl']} trail={fin['tr']}")
    results.append((cid, e["sym"], fin["status"], fin["pk"]))

print("\n==== SUMMARY ====")
for cid, sym, st, pk in results:
    print(f"  #{cid} {sym:<10} {st:<5} {pk}x")
print(f"repaired: {len(results)}/{len(VICTIMS)}")
