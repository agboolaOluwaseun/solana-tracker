"""One-off trail re-walk for Birdeye-rescued calls (2026-09-24).

Rescued verdicts (notes '(birdeye <chain>)' / 1m-confirmed) were judged on
Birdeye's token-address series, which never lands in price_cache — the
cache-only trailing attach can only refuse them. This script computes each
missing row's trail on the SAME Birdeye 1H series (scorer wick-clips
internally), anchored to the engine's stored entry dollar, and persists.

Cost: ~2 polite Birdeye requests per token (chain probe cached per
process; 1H window). ~190 total. Skips dust-scale entries (<1e-15: TECHMAN,
MOTION — no honest trail exists from a mixed-scale curve).
"""
import time
from datetime import datetime, timedelta, timezone

from db import get_connection
from pipeline import persist_trailing_result
from pricing.birdeye_rescue import find_chain, _ohlcv_points, enabled
from pricing.trailing import score_candles_trailing


def naive(s):
    t = datetime.fromisoformat(str(s).replace("Z", ""))
    return t.astimezone(timezone.utc).replace(tzinfo=None) if t.tzinfo else t


conn = get_connection()
if not enabled():
    raise SystemExit("birdeye rescue disabled/no key — cannot sweep")

rows = conn.execute("""
    SELECT cal.id, cal.token_symbol, cal.token_address, cal.chain,
           cal.call_timestamp, cal.entry_price_usd, cal.score_state, cal.note
    FROM calls cal
    LEFT JOIN trailing_results tr ON tr.call_id = cal.id
    WHERE cal.status IN ('win','loss')
      AND COALESCE(cal.note,'') LIKE '%birdeye%'
      AND COALESCE(cal.note,'') NOT LIKE '%excluded from SL/trail%'
      AND (tr.call_id IS NULL
           OR (tr.status='pending' AND COALESCE(tr.error,'') LIKE '%mismatch%'))
    ORDER BY cal.id""").fetchall()
print(f"{len(rows)} rescued calls to re-walk")

done = refused = dust = errs = 0
for i, r in enumerate(rows, start=1):
    entry = r["entry_price_usd"]
    if entry is None or entry < 1e-15:
        dust += 1
        continue
    ts = naive(r["call_timestamp"])
    try:
        bc = find_chain(r["token_address"], r["chain"] or "sol")
        if not bc:
            refused += 1
            continue
        t0 = int((ts - timedelta(hours=1)).replace(tzinfo=timezone.utc).timestamp()) // 3600 * 3600
        t1 = int((ts + timedelta(days=8)).replace(tzinfo=timezone.utc).timestamp())
        series = _ohlcv_points(r["token_address"], bc, t0, t1, "1H")
        if not series:
            refused += 1
            continue
        tr = score_candles_trailing(series, ts, entry_override=entry,
                                    pool_address=r["token_address"])
        if tr.status == "unpriceable_loss":
            refused += 1
            continue
        if r["score_state"] == "live" and tr.status == "expired":
            tr.status = "pending"
        # never clobber a decided row that ISN'T a refuse-pending: pending
        # rows written by the anchor guard are exactly what we're repairing
        existing = conn.execute(
            "SELECT status FROM trailing_results WHERE call_id=?",
            (r["id"],)).fetchone()
        if existing and existing["status"] in ("win", "loss", "expired"):
            refused += 1
            continue
        persist_trailing_result(r["id"], tr)
        done += 1
    except Exception as e:  # noqa: BLE001
        errs += 1
        print(f"  ! {r['id']} {r['token_symbol']}: {e}")
    if i % 15 == 0:
        print(f"  …{i}/{len(rows)} walked (done={done} refused={refused} "
              f"dust={dust} err={errs})")
        time.sleep(2)

print(f"\nsweep complete: {done} trail verdicts written, {refused} refused/"
      f"kept, {dust} dust-skipped, {errs} errors")
mix = conn.execute(
    "SELECT status, COUNT(*) n FROM trailing_results GROUP BY 1").fetchall()
print("trail table now:", [dict(m2) for m2 in mix])
