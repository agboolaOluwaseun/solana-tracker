"""Recover TRUE engine verdicts for museic + DRUGS (2026-09-24).

Replaces the manual 2.0x win overrides with what the production engine
and the pool-orientation guard actually compute on non-flipped data:

 museic (robinhood, window OPEN): token_meta seat reset to NULL ->
   guard lists GT pools, confirms the META pair is flipped, re-seats to
   the best base-seat GT pool, scores hourly+1m through evaluate_call_7d.
 DRUGS (sol, window CLOSED): token_meta seeded with exotic quote BIO ->
   guard confirms flipped, base-seat candidates all <$1000 reserve ->
   refuse + Birdeye (address-keyed) through the same engine.

Also cross-runs Birdeye for museic so both curves are visible.
Trailing: attach rides persist_stoploss_result for museic (its base pool
is now cached); for DRUGS it's computed on the Birdeye series directly
(flipped-curve cache must never decide a trail verdict).
"""
import logging
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

import pipeline
from db import init_db
from pipeline import (apply_eval7d, get_connection, persist_stoploss_result,
                      persist_trailing_result, price_one_call, transaction)

MUSEIC = dict(id=2791, chain="robinhood",
              addr="0xed97b68ae1be330963eb161a11beb1cf5fe71ba3",
              ts=datetime(2026, 9, 22, 15, 4, 9))
DRUGS = dict(id=2728, chain="sol",
             addr="3937Rsye62a53Y5o3UVG6gDPk8whAnKq1sxCV3Xu8YLP",
             ts=datetime(2026, 9, 13, 5, 20, 26))

init_db()

# ── prep token_meta so the guard sees the truth (and re-lists) ─────────────
with transaction() as c:
    c.execute("UPDATE token_meta SET pool_is_base=NULL, quote_symbol='META' "
              "WHERE chain='robinhood' AND address=?", (MUSEIC["addr"],))
    c.execute("UPDATE token_meta SET pool_is_base=NULL, quote_symbol='BIO' "
              "WHERE chain='sol' AND address=?", (DRUGS["addr"],))


def summarize(tag, r7):
    print(f"\n== {tag} ==")
    print("  status_plain:", r7.status_plain, "| status_stoploss:", r7.status_stoploss)
    print("  entry:", r7.entry_price_usd, "| max_multiple:", r7.max_multiple)
    print("  which:", r7.which_threshold_first, "| window_complete:", r7.window_complete)
    print("  time_2x:", r7.time_2x_reached, "| time_-50:", r7.time_minus_50_reached)
    print("  pool:", (r7.pool_address or "")[:24], "| note:", r7.note)


def now_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── museic: production path (guard re-seats to GT base pool) ───────────────
now = now_naive()
live_now = now if now < MUSEIC["ts"] + timedelta(days=7) else None
result, sl, r7 = price_one_call(MUSEIC["chain"], None, MUSEIC["addr"],
                                MUSEIC["ts"], now=live_now, allow_birdeye=True)
summarize("museic — GT re-seated (production path)", r7)

# cross-check: what does Birdeye say about museic?
from pricing.birdeye_rescue import try_rescue
rb = try_rescue(MUSEIC["addr"], "robinhood", MUSEIC["ts"], now=live_now)
if rb:
    summarize("museic — BIRDEYE cross-check", rb)

# ── DRUGS: production path (guard refuses flipped, Birdeye scores) ─────────
result2, sl2, r72 = price_one_call(DRUGS["chain"], None, DRUGS["addr"],
                                   DRUGS["ts"], now=None, allow_birdeye=True)
summarize("DRUGS — engine via Birdeye", r72)

# ── persist museic (live provisional; trailing attaches from base pool) ────
if r7.status_plain != "unpriceable_loss":
    apply_eval7d(MUSEIC["id"], r7)
    persist_stoploss_result(MUSEIC["id"], sl)   # -> trailing attach (cache)
    with transaction() as c:
        c.execute("UPDATE calls SET note='pool-guard recovered "
                  "(re-seated GT base pool) 2026-09-24' WHERE id=?",
                  (MUSEIC["id"],))
else:
    print("museic guard path returned unpriceable — NOT persisted")

# ── persist DRUGS: SL row first (trailing attach must NOT read the
#    flipped-curve cache), then correct trailing from Birdeye series ────────
from pricing.trailing import score_candles_trailing
apply_eval7d(DRUGS["id"], r72)
with transaction() as c:   # keep attach suppressed while SL persists
    c.execute("UPDATE calls SET note='excluded from SL/trail (birdeye "
              "trail computed manually)' WHERE id=?", (DRUGS["id"],))
persist_stoploss_result(DRUGS["id"], sl2)

from pricing.birdeye_rescue import find_chain, _ohlcv_points
bc = find_chain(DRUGS["addr"], "sol")
t0 = int((DRUGS["ts"] - timedelta(hours=1)).replace(tzinfo=timezone.utc)
         .timestamp()) // 3600 * 3600
t1 = int((DRUGS["ts"] + timedelta(days=8)).replace(tzinfo=timezone.utc)
         .timestamp())
hourly = _ohlcv_points(DRUGS["addr"], bc, t0, t1, "1H")
# trail may only see the call's own 7d window — no day-8 leakage
hourly = [p for p in hourly if p.timestamp < DRUGS["ts"] + timedelta(days=7)]
tr = score_candles_trailing(hourly, DRUGS["ts"])
print("\n== DRUGS trailing on Birdeye 1H ==", tr.status,
      "| peak", tr.peak_multiple, "| exit", tr.exit_multiple,
      "| hit_stop", tr.hit_trailing_stop)
persist_trailing_result(DRUGS["id"], tr)
with transaction() as c:
    c.execute("UPDATE calls SET note='pool-guard recovered via birdeye "
              "(flipped BIO/DRUGS pair) 2026-09-24' WHERE id=?",
              (DRUGS["id"],))

# ── final state check ──────────────────────────────────────────────────────
conn = get_connection()
for cid in (MUSEIC["id"], DRUGS["id"]):
    print("\ncall", cid, dict(conn.execute(
        "SELECT status, score_state, entry_price_usd, peak_multiple, "
        "max_multiple, note FROM calls WHERE id=?", (cid,)).fetchone()))
    print("  sl :", [dict(x) for x in conn.execute(
        "SELECT status, hit_stoploss, peak_profit_pct FROM stoploss_results "
        "WHERE call_id=?", (cid,))])
    print("  tr :", [dict(x) for x in conn.execute(
        "SELECT status, peak_multiple, exit_multiple, loss_pct FROM "
        "trailing_results WHERE call_id=?", (cid,))])

from analysis import windowed as W
print("\nCryptogem Hub (ch64) after recovery:")
for strat in ("normal", "stoploss", "trailing"):
    print(" ", strat, W.channel_stats_window(64, "all", strat, chain="all"))
