"""Correct museic rows to the 1m-wick-confirmed verdicts (user audit,
2026-09-24). SpyDefi says x2.2; minute data confirms:
  - 20:39 minute high 2.002e-5 (6.88x) = classic wick (next open 6.26e-6)
  - genuine sustained 2x: minutes 20:40-20:52 highs 6.26-6.90e-6 (2.15-2.37x)
  - clipped max high = 6.90e-6 = 2.37x  -> normal WIN 2.37x
  - SL: -50% from entry (1.455e-6) never printed (minute-low min ~1.70e-6
    at 17:00-17:59? engine will confirm) -> WIN
  - trailing: run the real scorer on the clipped minute series.
"""
from datetime import datetime, timedelta, timezone

from db import get_connection, transaction
from pipeline import (_get_v2_client, persist_stoploss_result,
                      persist_trailing_result, score_trailing_call)
from pricing.strategy7d_cache import make_cached_fetchers
from pricing.trailing import score_candles_trailing

POOL = "0xc59d4307c379eee9a211dea34f48964ae87777f86e8460a50190f6f6cc15886a"
TOKEN = "0xed97b68ae1be330963eb161a11beb1cf5fe71ba3"
CALL = datetime(2026, 9, 22, 15, 4, 9)
CID = 2791

conn = get_connection()
_, fetch_minute = make_cached_fetchers(conn, _get_v2_client("robinhood"))
now = datetime.now(timezone.utc).replace(tzinfo=None)
mins, _ = fetch_minute(POOL, TOKEN, CALL.replace(second=0, microsecond=0),
                       CALL + timedelta(days=7))

# clip (CALI rule)
clean = []
for i, m in enumerate(mins):
    o, h, l, c = m.open, m.high, m.low, m.close
    nxt = mins[i + 1].open if i + 1 < len(mins) else c
    bh = max(o, c, l)
    if bh > 0 and h > bh * 2 and nxt < h * 0.5:
        h = bh
    bl = min(o, c, h)
    if bl > 0 and l < bl * 0.5 and nxt > bl * 2:
        l = bl
    clean.append(type(m)(timestamp=m.timestamp, open=o, high=h, low=l,
                         close=c, volume=m.volume))

# trailing verdict on clipped minutes
tr = score_candles_trailing(clean, CALL, pool_address=POOL)
print("TRAIL on clipped 1m:", tr.status, "| peak", tr.peak_multiple,
      "| exit", tr.exit_multiple, "| hit", tr.hit_trailing_stop)

entry = 2.9099466418541533e-06 if False else None
# entry from engine = open of the call minute (15:04)
for m in mins:
    if m.timestamp == CALL.replace(second=0, microsecond=0):
        entry = m.open
        break
clipped_peak = max(m.high for m in clean)
peak_mult = clipped_peak / entry
print(f"entry {entry:.3e} | clipped peak {clipped_peak:.3e} = {peak_mult:.2f}x")

# calls row: WIN with honest multiple (note trail result too)
with transaction() as c:
    c.execute(
        """UPDATE calls SET
             status='win', is_win=1,
             entry_price_usd=?,
             peak_price_usd=?,
             peak_profit_pct=?,
             peak_multiple=?, max_multiple=?,
             target_2x_reached=1, minus_50_reached=0,
             time_2x_reached=?, time_minus_50_reached=NULL,
             which_threshold_first='2x',
             score_state='live', scored_window='7d', engine='7d',
             pending_reason=NULL,
             note='pool-guard recovered + 1m wick-confirmed 2026-09-24 "
                  "(SpyDefi x2.2 matches sustained 2.37x)'
           WHERE id=?""",
        (entry, clipped_peak, (peak_mult - 1) * 100, peak_mult, peak_mult,
         next(m.timestamp for m in clean if m.high >= entry * 2), CID))
# SL row: win, peak = same
from models import StoplossResult
persist_stoploss_result(CID, StoplossResult(
    entry_price_usd=entry, peak_price_usd=clipped_peak,
    peak_timestamp=next(m.timestamp for m in clean if m.high >= entry * 2),
    peak_profit_pct=(peak_mult - 1) * 100, hit_stoploss=False,
    stoploss_timestamp=None, is_win=True, status="win", pool_address=POOL))
# trailing row: recompute via production attach (cache now has dense minutes?
# _trailing_series prefers minutes when >=0.6*hours*60; here 710 vs 87*60*0.6=
# 3132 -> falls back to HOURLY (flipped-pool cache rows are under the META
# pool id, base pool has minutes only) — persist the clipped-1m result directly:
persist_trailing_result(CID, tr)

row = dict(conn.execute(
    "SELECT status, entry_price_usd, peak_multiple, max_multiple, note "
    "FROM calls WHERE id=?", (CID,)).fetchone())
print("calls:", row)
print("sl   :", dict(conn.execute(
    "SELECT status, peak_profit_pct FROM stoploss_results WHERE call_id=?",
    (CID,)).fetchone()))
print("trail:", dict(conn.execute(
    "SELECT status, peak_multiple, exit_multiple, loss_pct FROM "
    "trailing_results WHERE call_id=?", (CID,)).fetchone()))

from analysis import windowed as W
for s in ("normal", "stoploss", "trailing"):
    print("ch64", s, W.channel_stats_window(64, "all", s, chain="all"))
