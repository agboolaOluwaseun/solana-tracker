"""PLAGUE + SIF (Minty ch55) flip-repair — user report 2026-10-08.

Class: exotic-quote flipped pool (museic/DRUGS family), NEW sub-case the
guard missed: pool_is_base=SEATED token (quote_symbol='' on DS -> exotic
tripwire blind) whose GT curve sits in whole-dollar units — a 3x/13x
token scored as a 1.0x loss on the OTHER asset's curve.
Detection proof (this session): GT /tokens/{mint}/pools for both mints:
  PLAGUE: PFE/PLAGUE pool $27.77 (what we scored) vs PLAGUE/SOL $9.1e-6
          (fdv $9.1k, the REAL token the channel called; user saw ~3x)
  SIF:    DJT/SIF pool $8.21  (what we scored) vs SIF/SOL pools $6e-5
          (fdv $60k, user says ~13x)
Repair: mint-first pool pick (highest-reserve BASE-seat pool), pool-
orientation flip (quote=the called token), clear contaminated cache rows
(stamped to the wrong pool), write corrected token_meta seat, reprice
through the production engine with rescue allowed (these ARE initial-
scan pricing), verify verdict flipped.

NOTE: engine-level seat tripwire + boot-mint-first fix ship separately
(tests pin them)."""
import sys
sys.path.insert(0, ".")
import sqlite3
from datetime import datetime, timezone, timedelta

import pipeline
from pipeline import price_one_call, apply_eval7d, persist_stoploss_result, upsert_token_meta
from pricing.geckoterminal import GeckoTerminalClient

# (call_id, mint, old_pool) — the two confirmed victims
VICTIMS = [
    (3306, "25moUjJpSN73D9NPwD4VmcyR4FVTWiYaL9XPKkdta5iP",
         "BdzJKvhg2RkL8gMHeZ7rNKwC1VtjmwBBhQfJRAr3yr4D"),
    (3305, "FgGwBhF9fpyTR25gzuzDsaf2STRdTqCV2GiL8ZYj3rYr",
         "da9syNoAgPBE1dQ3qzSMMLnHTqWV6c64XRZ6C8ZGt9m"),
]

conn = pipeline.get_connection()
gt = GeckoTerminalClient()
now = datetime.now(timezone.utc).replace(tzinfo=None)

for call_id, mint, old_pool in VICTIMS:
    print(f"\n== call {call_id} {mint[:12]}… ==")
    # 1. GT seat listing: find mint-as-BASE pools, pick highest reserve
    data = gt._request(f"/networks/solana/tokens/{mint}/pools", params={"page": 1})
    cands = []
    for p in (data or {}).get("data", []):
        attrs = p.get("attributes") or {}
        rel = p.get("relationships") or {}
        base_id = ((rel.get("base_token") or {}).get("data") or {}).get("id", "")
        pid = str(p.get("id", ""))
        pool_addr = pid.split("solana_", 1)[-1] if pid.startswith("solana_") else pid
        if base_id.endswith(mint) or mint in base_id:
            liq = float(attrs.get("reserve_in_usd") or 0)
            if liq >= 1000:
                cands.append((pool_addr, liq, attrs.get("name", "")))
    # clear contaminated cache rows FIRST (both paths): token-primary rows
    # are keyed by the MINT with the wrong pool stamped — a reseat in
    # token_meta alone would still read the contaminated curve from cache
    with pipeline.transaction() as tc:
        n = tc.execute(
            "DELETE FROM price_cache WHERE chain='sol' AND token_address=? "
            "AND pool_address=?", (mint, old_pool)).rowcount
    print(f"  cleared {n} contaminated cache rows")
    if not cands:
        print("  !! no base-seat pool >= $1k reserve — Birdeye path")
        new_pool = None
    else:
        cands.sort(key=lambda x: -x[1])
        new_pool, liq, nm = cands[0]
        print(f"  re-seat -> {new_pool[:16]}… ({nm}, ${liq:,.0f} reserve)")

        # 2. flip the orientation fields (this pool: mint=BASE, so name is
        #    'MINT/SOL' — quote symbol is the SECOND token)
        name_parts = nm.split("/")
        quote_sym = name_parts[1] if len(name_parts) == 2 else ""

        # 4. corrected identity + seat, cache-clean for the engine
        with pipeline.transaction() as tc:
            tc.execute("UPDATE token_meta SET dex_pool_id=?, pool_address=?, "
                       "pool_is_base=1, quote_symbol=? WHERE address=? AND chain='sol'",
                       (new_pool, new_pool, quote_sym, mint))
            tc.execute("UPDATE calls SET pool_address=?, entry_price_usd=NULL, "
                       "peak_price_usd=NULL, peak_multiple=NULL, is_win=NULL, "
                       "status='pending', pending_reason=NULL, scored_window=NULL, "
                       "score_state=NULL, note=NULL WHERE id=?", (new_pool, call_id))

    # 5. reprice through the production engine (rescue allowed — initial-
    #    scan-class operator action; the birdeye series is seat-immune)
    ts = datetime.fromisoformat(str(conn.execute(
        "SELECT call_timestamp FROM calls WHERE id=?", (call_id,)).fetchone()["call_timestamp"]).replace("Z", ""))
    res, sl, r7 = price_one_call("sol", gt, mint, ts, now=now, allow_birdeye=True)
    if r7 is None:
        print("  !! legacy result — aborting persist")
        continue
    apply_eval7d(call_id, r7)
    persist_stoploss_result(call_id, sl)
    row = conn.execute("SELECT status, score_state, round(peak_multiple,2) pk, "
                       "substr(COALESCE(note,''),1,40) note FROM calls WHERE id=?",
                       (call_id,)).fetchone()
    print(f"  -> {row['status']} state={row['score_state']} peak={row['pk']}x note={row['note']!r}")
    print(f"     (source note above shows birdeye when GT pool still blind)")
print("\ndone")
