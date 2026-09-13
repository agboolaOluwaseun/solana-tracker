"""
New 7-day Token Call Evaluation Strategy — exact-logic implementation.

Per call (all times UTC, naive):
  1. REQUEST 1 (hourly, always): hourly OHLCV [call-hour .. call+7d].
       - call-hour candle = the hourly candle whose window contains call_ts.
       - screening_entry = LOW of call-hour candle; screening_target = 2x LOW.
       - max_hourly_high = max HIGH over call-hour candle + all subsequent
         hourly candles within [call_ts, call_ts+7d].
       - If max_hourly_high < screening_target  ->  DEFINITE LOSS, 1 request.
  2. REQUEST 2 (minute, only if screening passed): minute OHLCV for the
     call-hour remainder only (call minute .. hour end), resolving the
     ambiguous call-hour candle.
  3. Discard the call-hour hourly candle. Final post-call path =
       [call-minute .. hour end] minutes  +  [next hour .. day 7] hourlies.
  4. actual_entry = OPEN of the call-minute candle; actual_target = 2x entry.
     Chronological walk: first time high >= target (t_2x) and first time
     low  <= entry*0.5 (t_minus50).  Which came first?
       - both first occur in the SAME MINUTE candle   -> WIN for all
         strategies (user rule; 2x-then-drop far more likely than
         drop-then-2x inside one minute)
       - both first occur in the same HOURLY candle   -> REQUEST 3: minute
         data for that hour; resolve order; same-minute rule applies again
       - 2x first        -> plain WIN + stoploss WIN
       - minus50 first   -> plain WIN (2x reached), stoploss LOSS
       - only 2x         -> WIN both
       - only minus50    -> LOSS both
       - neither         -> LOSS both

Caching (aggregate-aware, test DB only):
  - eval_candles(pool, token, aggregate, candle_ts) — minutes and hours
    never collide on the same key.
  - Hourly candles are cached EXCEPT the call-hour candle (never trusted,
    never persisted; re-runs refetch it, ~1 request/call).
  - Minute candles fully cached (call-hour remainder + any resolved hour).

Usage: python scripts/eval_strategy.py <channel_tag> [label] [limit] [call_ids]
  channel_tag: 666 | trenches | eleetmo
  label:        start-time label for the report (default: now ISO)
  limit:        optional max number of calls (smoke testing)
  call_ids:     optional comma list of call ids to evaluate (targeted re-run)
"""
import sys
import time
import sqlite3
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
sys.path.insert(0, str(Path(__file__).parent.parent))

CHANNEL = sys.argv[1] if len(sys.argv) > 1 else "666"
LABEL = sys.argv[2] if len(sys.argv) > 2 else datetime.utcnow().isoformat()
LIMIT = int(sys.argv[3]) if len(sys.argv) > 3 else 0
CALL_IDS = [int(x) for x in sys.argv[4].split(",")] if len(sys.argv) > 4 and sys.argv[4] else None

TEST_DB = Path(__file__).parent.parent / "test_eval.db"
conn = sqlite3.connect(TEST_DB, isolation_level=None)  # autocommit
conn.row_factory = sqlite3.Row

from models import PricePoint
from pricing.geckoterminal_v2 import GeckoTerminalClientV2, _strip_network_prefix, _parse_ohlcv

client = GeckoTerminalClientV2()

# ---------------- candle cache (aggregate-aware) ----------------

def _iso_ts(ts: datetime) -> str:
    return ts.replace(microsecond=0).isoformat() + "Z"

def load_cached(pool: str, token: str, agg: str, start: datetime, end: datetime):
    rows = conn.execute(
        "SELECT candle_ts, open, high, low, close, volume FROM eval_candles "
        "WHERE pool_address=? AND token_address=? AND aggregate=? "
        "AND candle_ts >= ? AND candle_ts < ? ORDER BY candle_ts",
        (pool, token, agg, _iso_ts(start), _iso_ts(end)),
    ).fetchall()
    return [
        PricePoint(
            timestamp=datetime.fromisoformat(r["candle_ts"].replace("Z", "")),
            open=r["open"], high=r["high"], low=r["low"],
            close=r["close"], volume=r["volume"],
        )
        for r in rows
    ]

def store_candles(pool: str, token: str, agg: str, candles) -> None:
    for c in candles:
        conn.execute(
            "INSERT OR REPLACE INTO eval_candles (pool_address, token_address, aggregate, "
            "candle_ts, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?,?,?)",
            (pool, token, agg, _iso_ts(c.timestamp), c.open, c.high, c.low, c.close, c.volume),
        )

def _fetch_span(pool: str, token: str, agg: str, start: datetime, end: datetime) -> tuple[list, int]:
    """One GT request (GT ignores after_timestamp; returns latest N before end)."""
    params = {
        "before_timestamp": int(end.replace(tzinfo=timezone.utc).timestamp()),
        "after_timestamp": int(start.replace(tzinfo=timezone.utc).timestamp()),
        "limit": 1000,
        "currency": "usd",
    }
    data = client._request(f"/networks/{client.network}/pools/{pool}/ohlcv/{agg}", params=params)
    candles = [c for c in _parse_ohlcv(data) if start <= c.timestamp < end]
    candles.sort(key=lambda c: c.timestamp)
    return candles, 1

def fetch_minute_span(pool: str, token: str, start: datetime, end: datetime) -> tuple[list, int]:
    """Minute candles for [start,end) with full caching (re-runs are free)."""
    cached = load_cached(pool, token, "minute", start, end)
    expected = max(0, int((end - start).total_seconds() // 60))
    if len(cached) >= expected and cached:
        return cached, 0
    fresh, reqs = _fetch_span(pool, token, "minute", start, end)
    store_candles(pool, token, "minute", fresh)
    merged = {_iso_ts(c.timestamp): c for c in cached + fresh}
    out = [merged[k] for k in sorted(merged)]
    return out, reqs

def fetch_hourly_window(pool: str, token: str, hour_start: datetime, end7: datetime) -> tuple[list, int]:
    """Hourly candles for [hour_start, end7).  Cache stores ALL EXCEPT the
    call-hour candle (token of the first candle); screening always needs it
    fresh, so re-runs keep costing exactly one hourly request per call."""
    candles, reqs = _fetch_span(pool, token, "hour", hour_start, end7)
    # persist everything except the first (call-hour) candle
    if candles:
        store_candles(pool, token, "hour", candles[1:])
    return candles, reqs

# ---------------- chronological evaluation ----------------

def resolve_order_in_hour(pool, token, hour_candle, entry, target, stop):
    """REQUEST 3: minute data for one ambiguous hourly candle.
    Returns which_first: '2x' | 'minus50' | 'same_candle', plus reqs used."""
    h_start = hour_candle.timestamp.replace(minute=0, second=0, microsecond=0)
    h_end = h_start + timedelta(hours=1)
    minutes, reqs = fetch_minute_span(pool, token, h_start, h_end)
    t2x = None
    t50 = None
    for m in minutes:
        if t2x is None and m.high >= target:
            t2x = m.timestamp
        if t50 is None and m.low <= stop:
            t50 = m.timestamp
        if t2x is not None and t50 is not None:
            break
    if t2x is None and t50 is None:
        # minute data contradicting the hourly candle — trust the hour,
        # treat as same-candle (user rule: win for all)
        return "same_candle", reqs
    if t2x is not None and t50 is not None and t2x == t50:
        return "same_candle", reqs
    if t2x is not None and (t50 is None or t2x < t50):
        return "2x", reqs
    return "minus50", reqs


def evaluate_call(call) -> dict:
    token = call["token_address"]
    meta = conn.execute(
        "SELECT dex_pool_id FROM token_meta WHERE address=?", (token,)
    ).fetchone()
    pool = (meta["dex_pool_id"] if meta and meta["dex_pool_id"] else None) or call["pool_address"]
    if not pool:
        return {"id": call["id"], "symbol": call["token_symbol"], "time": call["call_timestamp"][:16],
                "plain": "unpriceable_loss", "stoploss": "unpriceable_loss", "reqs": 0,
                "note": "no pool"}
    pool = _strip_network_prefix(pool, client.network)

    call_ts_naive = datetime.fromisoformat(call["call_timestamp"].replace("Z", "+00:00")).replace(tzinfo=None)
    hour_start = call_ts_naive.replace(minute=0, second=0, microsecond=0)
    hour_end = hour_start + timedelta(hours=1)
    minute_floor = call_ts_naive.replace(second=0, microsecond=0)
    end7 = call_ts_naive + timedelta(days=7)

    # ---- REQUEST 1: hourly ----
    try:
        hourly, reqs = fetch_hourly_window(pool, token, hour_start, end7)
    except Exception as e:  # noqa: BLE001
        return {"id": call["id"], "symbol": call["token_symbol"], "time": call["call_timestamp"][:16],
                "plain": "unpriceable_loss", "stoploss": "unpriceable_loss", "reqs": 0,
                "note": f"hourly fetch failed: {type(e).__name__}"}

    call_hour_candle = next((c for c in hourly if c.timestamp == hour_start), None)
    if call_hour_candle is None:
        return {"id": call["id"], "symbol": call["token_symbol"], "time": call["call_timestamp"][:16],
                "plain": "unpriceable_loss", "stoploss": "unpriceable_loss", "reqs": reqs,
                "note": "no call-hour candle"}

    # ---- screening test ----
    screening_entry = call_hour_candle.low
    screening_target = screening_entry * 2.0
    max_hourly_high = max((c.high for c in hourly if hour_start <= c.timestamp < end7), default=0.0)

    if max_hourly_high < screening_target:
        return {"id": call["id"], "symbol": call["token_symbol"], "time": call["call_timestamp"][:16],
                "plain": "loss", "stoploss": "loss", "reqs": reqs, "note": "screening LOSS",
                "screen_entry": screening_entry, "screen_target": screening_target,
                "max_h": max_hourly_high, "entry": None}

    # ---- REQUEST 2: call-hour remainder minutes ----
    granular = True
    try:
        minutes, reqs2 = fetch_minute_span(pool, token, minute_floor, hour_end)
    except Exception as e:  # noqa: BLE001
        return {"id": call["id"], "symbol": call["token_symbol"], "time": call["call_timestamp"][:16],
                "plain": "unpriceable_loss", "stoploss": "unpriceable_loss", "reqs": reqs,
                "note": f"minute fetch failed: {type(e).__name__}"}
    reqs += reqs2

    entry_candle = next((m for m in minutes if m.timestamp == minute_floor), None)
    if entry_candle is None:
        entry_candle = next((m for m in minutes if m.timestamp <= call_ts_naive < m.timestamp + timedelta(minutes=1)), None)
    # OPTION 2 (user-approved): no candle at the exact call minute — the pool
    # started trading just after the call.  Use the first available minute
    # candle within 5 minutes AFTER the call as the entry (open), matching
    # what followers actually could have bought.
    opt2_entry = False
    if entry_candle is None:
        for m in minutes:
            if call_ts_naive < m.timestamp <= call_ts_naive + timedelta(minutes=5):
                entry_candle = m
                opt2_entry = True
                break
    if entry_candle is None or not entry_candle.open or entry_candle.open <= 0:
        return {"id": call["id"], "symbol": call["token_symbol"], "time": call["call_timestamp"][:16],
                "plain": "unpriceable_loss", "stoploss": "unpriceable_loss", "reqs": reqs,
                "note": "no call-minute candle"}

    entry = entry_candle.open
    target = entry * 2.0
    stop = entry * 0.5

    # ---- final chronological path: call-hour minutes + post-hour hourlies ----
    path = [m for m in minutes if m.timestamp >= entry_candle.timestamp]
    path += [c for c in hourly if c.timestamp >= hour_end and c.timestamp < end7]
    path.sort(key=lambda c: c.timestamp)

    t2x = None
    t50 = None
    which_first = "none"
    extra_reqs = 0
    for c in path:
        both_before = (t2x is None and t50 is None)
        if t2x is None and c.high >= target:
            t2x = c.timestamp
        if t50 is None and c.low <= stop:
            t50 = c.timestamp
        if both_before and t2x is not None and t50 is not None:
            # first occurrence of BOTH thresholds in THIS candle
            if c.timestamp < hour_end:
                # minute candle -> same-candle win for all (user rule)
                which_first = "same_candle"
            else:
                # hourly candle -> REQUEST 3 to resolve the hour
                which_first, reqs3 = resolve_order_in_hour(pool, token, c, entry, target, stop)
                extra_reqs += reqs3
            break
        if t2x is not None and t50 is not None:
            which_first = "2x" if t2x < t50 else "minus50"
            break

    reqs += extra_reqs
    if which_first == "none":
        if t2x is not None:
            which_first = "2x"
        elif t50 is not None:
            which_first = "minus50"

    plain = "win" if t2x is not None else "loss"
    if t2x is not None and which_first == "same_candle":
        stoploss = "win"                      # user rule
    elif which_first == "2x":
        stoploss = "win"
    elif which_first == "minus50":
        stoploss = "loss" if t2x is not None else "loss"
    else:
        stoploss = "loss"

    max_price = max((c.high for c in path), default=entry)
    min_price = min((c.low for c in path), default=entry)
    max_mult = max_price / entry if entry else None
    drawdown = (1.0 - min_price / entry) * 100 if entry else None

    return {
        "id": call["id"], "symbol": call["token_symbol"], "time": call["call_timestamp"][:16],
        "plain": plain, "stoploss": stoploss, "reqs": reqs, "note": which_first,
        "screen_entry": screening_entry, "screen_target": screening_target,
        "max_h": max_hourly_high, "entry": entry, "target": target,
        "max_mult": max_mult, "drawdown": drawdown, "granular": granular,
        "t2x": t2x, "t50": t50, "pool": pool,
        "end": end7,
    }


# ---------------- main ----------------
calls = conn.execute(
    "SELECT id, token_address, token_symbol, call_timestamp, pool_address "
    "FROM calls WHERE channel_tag=? ORDER BY call_timestamp", (CHANNEL,)
).fetchall()
if CALL_IDS:
    calls = [c for c in calls if c["id"] in CALL_IDS]
if LIMIT:
    calls = calls[:LIMIT]

start_time = time.monotonic()
print(f"[{LABEL}] {CHANNEL}: evaluating {len(calls)} calls (screening + optional minute resolution)...\n")

results = []
for idx, call in enumerate(calls, 1):
    r = evaluate_call(call)
    results.append(r)
    if idx % 5 == 0:
        elapsed = time.monotonic() - start_time
        eta = (elapsed / idx) * (len(calls) - idx)
        print(f"  {idx}/{len(calls)} | {elapsed/60:.1f} min elapsed | ETA {eta/60:.1f} min")

elapsed = time.monotonic() - start_time

# ---- report ----
print(f"\n{'='*80}")
print(f"7-DAY EVALUATION STRATEGY — {CHANNEL} (started {LABEL})")
print(f"{'='*80}")
print(f"Finished in {elapsed:.0f}s ({elapsed/60:.1f} min) — {len(calls)} calls\n")

pred = [r for r in results if r["plain"] in ("win", "loss")]
pwins = sum(1 for r in pred if r["plain"] == "win")
swins = sum(1 for r in pred if r["stoploss"] == "win")
unp = [r for r in results if r["plain"] == "unpriceable_loss"]
print(f"Plain strategy:    {pwins}W / {len(pred)-pwins}L / {len(unp)} unp  ->  "
      f"{pwins/len(pred)*100 if pred else 0:.1f}% WR")
print(f"Stoploss strategy: {swins}W / {len(pred)-swins}L / {len(unp)} unp  ->  "
      f"{swins/len(pred)*100 if pred else 0:.1f}% WR")
reqs_total = sum(r["reqs"] for r in results)
gran = sum(1 for r in results if r.get("granular"))
same_candle = sum(1 for r in results if r["note"] == "same_candle")
req3 = sum(1 for r in results if r["reqs"] >= 3)
print(f"API requests: {reqs_total} total, {reqs_total/len(calls):.2f}/call | "
      f"granular analysis: {gran}/{len(calls)} | 3rd-request hours: {req3} | same-candle wins: {same_candle}\n")

# comparison vs legacy
print(f"{'ID':>4} {'Sym':<12} {'Call':<17} {'Leg12h':<8} {'Leg7d':<8} {'New':<5} {'Stopl':<6} {'Mult':>6}  Note")
print("-" * 84)
for r in sorted(results, key=lambda x: x["time"]):
    old = conn.execute(
        "SELECT legacy_status, legacy_7d_status, legacy_entry_price FROM calls WHERE id=? AND channel_tag=?",
        (r["id"], CHANNEL),
    ).fetchone()
    leg12 = (old["legacy_status"] or "?").replace("unpriceable_loss", "unp") if old else "?"
    leg7 = (old["legacy_7d_status"] or "?").replace("unpriceable_loss", "unp") if old else "?"
    mult = f"{r['max_mult']:.2f}x" if r.get("max_mult") is not None else "  -  "
    print(f"{r['id']:>4} {str(r['symbol'] or '?'):<12} {r['time']:<17} {leg12:<8} {leg7:<8} "
          f"{r['plain']:<5} {r['stoploss']:<6} {mult:>6}  {r['note']}")

# CSV
csv_path = TEST_DB.parent / f"eval_{CHANNEL}.csv"
with open(csv_path, "w") as f:
    f.write("ID,Symbol,Call Time,Legacy 12h,Legacy 7d,Plain Status,Stoploss Status,"
            "Entry,Target,Max Multiple,Max Drawdown %,Screening Entry,T2x Time,T-50 Time,"
            "Which First,API Requests,Granular,Note\n")
    for r in sorted(results, key=lambda x: x["time"]):
        old = conn.execute(
            "SELECT legacy_status, legacy_7d_status FROM calls WHERE id=? AND channel_tag=?",
            (r["id"], CHANNEL),
        ).fetchone()
        leg12 = (old["legacy_status"] or "") if old else ""
        leg7 = (old["legacy_7d_status"] or "") if old else ""
        t2x = r["t2x"].strftime("%Y-%m-%dT%H:%M") if r.get("t2x") else ""
        t50 = r["t50"].strftime("%Y-%m-%dT%H:%M") if r.get("t50") else ""
        f.write(
            f"{r['id']},{str(r['symbol'] or '?')},{r['time']},{leg12},{leg7},{r['plain']},{r['stoploss']},"
            f"{r.get('entry') or ''},{r.get('target') or ''},{r.get('max_mult') or ''},"
            f"{r.get('drawdown') or ''},{r.get('screen_entry') or ''},{t2x},{t50},{r['note']},"
            f"{r['reqs']},{r.get('granular', 0)},{r.get('note','')}\n"
        )
print(f"\nCSV: {csv_path}")