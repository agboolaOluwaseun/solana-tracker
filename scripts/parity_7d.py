"""
scripts/parity_7d.py — Parity harness: pricing/strategy7d.py vs the validated
prototype run (test_eval.db eval_candles cache + eval_results for channel 666).

ZERO API requests: replay fetchers serve strictly from the cached candles.
The call-hour hourly candle was intentionally never persisted (ambiguous —
screening uses it fresh), so for replay we reconstruct its screening effect:
  - screening_entry / screening_target / actual entry come from the stored
    eval_results row (the exact numbers the prototype used);
  - synthetic call-hour HIGH = max(2*screening_entry, max high of cached
    minute candles in the call-hour remainder) — both are proven lower bounds
    of the real candle high whenever the prototype took that path, so the
    replay can only pass screening when the real run did;
  - the final walk discards the call-hour candle exactly like the engine does.
Request counts: replay costs 1 hourly/call; minute requests are verified
separately (granular rows must have their call-hour minute remainder cached).

Exit 0 + 'MISMATCHES: 0' only when every decision, entry, multiple, drawdown,
timing and which-first matches the prototype.
"""
import csv
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from models import PricePoint                      # noqa: E402
from pricing.strategy7d import evaluate_call_7d    # noqa: E402

DB = ROOT / "test_eval.db"
CSV_PATH = ROOT / "eval_666.csv"
CHANNEL = "666"


def iso(ts: datetime) -> str:
    return ts.replace(microsecond=0).isoformat() + "Z"


def main() -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    stored = {
        r["id"]: r
        for r in conn.execute(
            "SELECT * FROM eval_results WHERE channel_tag=?", (CHANNEL,)
        ).fetchall()
    }
    if len(stored) != 88:
        print(f"FAIL: expected 88 stored eval_results rows for {CHANNEL}, got {len(stored)}")
        return 1

    csv_rows = list(csv.DictReader(open(CSV_PATH)))
    if len(csv_rows) != 88:
        print(f"FAIL: eval_666.csv has {len(csv_rows)} rows, expected 88")
        return 1

    mismatches = []
    per_call = []
    skipped_fetch_error = 0

    for row in csv_rows:
        cid = int(row["ID"])
        s = stored[cid]
        # Prototype row whose hourly fetch itself failed: no candles were ever
        # cached, so a zero-API replay cannot reproduce it. Count separately.
        if (row["Which First"] or "").startswith("hourly fetch failed"):
            skipped_fetch_error += 1
            per_call.append((cid, "unpriceable_loss", "unpriceable_loss", "fetch-error(skip)"))
            continue
        call = conn.execute(
            "SELECT token_address, pool_address FROM calls WHERE channel_tag=? AND id=?",
            (CHANNEL, cid),
        ).fetchone()
        meta = conn.execute(
            "SELECT dex_pool_id FROM token_meta WHERE address=?", (call["token_address"],)
        ).fetchone()
        pool = (meta["dex_pool_id"] if meta and meta["dex_pool_id"] else None) or call["pool_address"]
        token = call["token_address"]
        call_ts = datetime.fromisoformat(s["call_timestamp"].replace("Z", "+00:00")).replace(tzinfo=None)
        hour_start = call_ts.replace(minute=0, second=0, microsecond=0)
        hour_end = hour_start + timedelta(hours=1)
        end7 = call_ts + timedelta(days=7)

        # ---- cached candles (read-only replay) ----
        hours = [
            PricePoint(
                timestamp=datetime.fromisoformat(r["candle_ts"].replace("Z", "")),
                open=r["open"], high=r["high"], low=r["low"],
                close=r["close"], volume=r["volume"],
            )
            for r in conn.execute(
                "SELECT * FROM eval_candles WHERE pool_address=? AND token_address=? "
                "AND aggregate='hour' ORDER BY candle_ts", (pool, token)
            ).fetchall()
        ]
        mins = [
            PricePoint(
                timestamp=datetime.fromisoformat(r["candle_ts"].replace("Z", "")),
                open=r["open"], high=r["high"], low=r["low"],
                close=r["close"], volume=r["volume"],
            )
            for r in conn.execute(
                "SELECT * FROM eval_candles WHERE pool_address=? AND token_address=? "
                "AND aggregate='minute' ORDER BY candle_ts", (pool, token)
            ).fetchall()
        ]

        # ---- reconstruct the call-hour candle's screening effect ----
        screen_entry = s["screening_entry_price"]
        screen_target = s["screening_target"] or (screen_entry * 2.0 if screen_entry else None)
        # The CSV's Granular column is unreliable (0 for every row — lost in the
        # Option-2 merge). Ground truth for "request 2 happened" is an entry price.
        granular_proto = bool(row["Entry"].strip())
        callhour_minutes = [m for m in mins if hour_start <= m.timestamp < hour_end]
        if granular_proto and screen_entry and screen_target:
            # prototype passed screening (request 2 happened), so real call-hour
            # high >= target; both target and the cached minute highs are proven
            # lower bounds of the real high -> synthetic passes screening here too.
            synth_high = max(screen_target,
                             max((m.high for m in callhour_minutes), default=0.0))
            hours = [PricePoint(timestamp=hour_start, open=screen_entry,
                                high=synth_high, low=screen_entry, close=screen_entry,
                                volume=0.0)] + hours
        elif screen_entry is not None and screen_target and screen_target > 0:
            # screening-loss row: real high < target -> synthetic just under it
            hours = [PricePoint(timestamp=hour_start, open=screen_entry,
                                high=screen_target * 0.999, low=screen_entry,
                                close=screen_entry, volume=0.0)] + hours
        if granular_proto and not screen_entry:
            mismatches.append((cid, "missing screening numbers for a granular row"))

        minute_calls: list = []

        def fetch_hourly(p, t, start, end, _hours=hours):
            out = [c for c in _hours if start <= c.timestamp < end]
            out.sort(key=lambda c: c.timestamp)
            return out, 1  # replay: the one unavoidable hourly request per call

        def fetch_minute(p, t, start, end, _mins=mins, _mc=minute_calls):
            _mc.append((start, end))
            out = [m for m in _mins if start <= m.timestamp < end]
            out.sort(key=lambda m: m.timestamp)
            return out, 0  # fully cached in the prototype run

        r = evaluate_call_7d(pool, token, call_ts, fetch_hourly, fetch_minute)

        # ---- compare against the prototype CSV + eval_results ----
        def cmp(name, got, want):
            if want in (None, "", "None"):
                want = None
            if isinstance(want, str) and want == "":
                want = None
            if got in (None, "", "None"):
                got = None
            if got != want:
                mismatches.append((cid, f"{name}: replay={got!r} prototype={want!r}"))

        cmp("plain", r.status_plain, row["Plain Status"].strip())
        cmp("stoploss", r.status_stoploss, row["Stoploss Status"].strip())
        # the prototype CSV stores note (= which_first for decided rows, the
        # failure note for unpriceable, 'screening LOSS' for screening) here
        cmp("which_first/note", r.note, row["Which First"].strip())

        def fnum(x):
            try:
                return float(x)
            except (TypeError, ValueError):
                return None

        cmp("entry", r.entry_price_usd, fnum(row["Entry"]))
        cmp("max_mult", r.max_multiple, fnum(row["Max Multiple"]))
        cmp("t2x", r.time_2x_reached.strftime("%Y-%m-%dT%H:%M") if r.time_2x_reached else "",
            row["T2x Time"].strip())
        cmp("t50", r.time_minus_50_reached.strftime("%Y-%m-%dT%H:%M") if r.time_minus_50_reached else "",
            row["T-50 Time"].strip())
        # drawdown only present for decided granular rows
        dd = fnum(row["Max Drawdown %"])
        if dd is not None and r.max_drawdown_pct is not None:
            cmp("drawdown", round(r.max_drawdown_pct, 6), round(dd, 6))
        cmp("granular", bool(r.granular_analysis_required), granular_proto)
        # NB: prototype reqs (CSV) under-count on minute-cache hits from the
        # smoke/rerun history, so exact req equality is NOT asserted; the path
        # comparison above (statuses, timings, which-first, granular) is the parity.

        per_call.append((cid, r.status_plain, r.status_stoploss, r.note))

    # ---- totals vs validated reference numbers ----
    decided = [p for p in per_call if p[1] in ("win", "loss")]
    pw = sum(1 for p in decided if p[1] == "win")
    sw = sum(1 for p in decided if p[2] == "win")
    unp = sum(1 for p in per_call if p[1] == "unpriceable_loss")
    one_req = sum(1 for row in csv_rows if int(row["API Requests"]) == 1)
    two_req = sum(1 for row in csv_rows if int(row["API Requests"]) == 2)
    three_req = sum(1 for row in csv_rows if int(row["API Requests"]) >= 3)

    print(f"calls={len(per_call)} decided={len(decided)}")
    print(f"plain:    {pw}W/{len(decided)-pw}L/{unp}unp  ({pw/len(decided)*100:.1f}%)")
    print(f"stoploss: {sw}W/{len(decided)-sw}L/{unp}unp  ({sw/len(decided)*100:.1f}%)")
    print(f"requests (prototype): {one_req}x1 + {two_req}x2 + {three_req}x3 = "
          f"{one_req + 2*two_req + 3*three_req}")

    if mismatches:
        print(f"\nMISMATCHES: {len(mismatches)}")
        for cid, msg in mismatches:
            print(f"  call {cid}: {msg}")
        return 1
    print("\nMISMATCHES: 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
