"""One-time: convert held-to-cutoff losses to the 'expired' convention.

User rule 2026-09: for the STOP strategies a verdict exists only when one
of the two triggers fired. Rows written by the old scorer as
status='loss' with the stop never triggered (hit_stoploss=0 /
hit_trailing_stop=0) are NOT losses — they are undecided. Closed-window
ones become 'expired' (gray in UI, excluded from stats); live-window ones
become 'pending' (the next rescore retries them naturally).

Also computes the mark-to-market (final close) the gray tiles show, from
price_cache — the same cache-only walk trailing uses. Zero API.
"""
from datetime import datetime, timedelta, timezone

from db import get_connection


def _naive(s):
    ts = datetime.fromisoformat(str(s).replace("Z", ""))
    return ts.astimezone(timezone.utc).replace(tzinfo=None) if ts.tzinfo else ts


def _last_close(conn, pool, ts, days=7):
    end = ts + timedelta(days=days)
    rows = conn.execute(
        "SELECT close, candle_ts FROM price_cache WHERE pool_address=? "
        "AND aggregate='hour' AND candle_ts>=? AND candle_ts<? "
        "ORDER BY candle_ts DESC LIMIT 1",
        (pool, _iso(ts - timedelta(hours=1)), _iso(end))).fetchall()
    if not rows:  # minute-only pools
        rows = conn.execute(
            "SELECT close, candle_ts FROM price_cache WHERE pool_address=? "
            "AND aggregate='minute' AND candle_ts>=? AND candle_ts<? "
            "ORDER BY candle_ts DESC LIMIT 1",
            (pool, _iso(ts - timedelta(hours=1)), _iso(end))).fetchall()
    if not rows:
        return None, None
    return rows[0]["close"], rows[0]["candle_ts"]


def _iso(dt):
    return dt.replace(microsecond=0).isoformat() + "Z"


def main():
    conn = get_connection()
    changed = skip_no_pool = skip_no_candles = 0

    # ── stoploss_results ────────────────────────────────────────────────
    held = conn.execute("""
        SELECT sr.call_id, sr.entry_price_usd, cal.pool_address,
               cal.call_timestamp, cal.score_state
        FROM stoploss_results sr JOIN calls cal ON cal.id = sr.call_id
        WHERE sr.status='loss' AND sr.hit_stoploss=0
          AND cal.status IN ('win','loss')""").fetchall()
    for r in held:
        ts = _naive(r["call_timestamp"])
        if ts.tzinfo is not None:
            ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
        live = r["score_state"] == "live"
        new_status = "pending" if live else "expired"
        fc = fts = None
        if not live:
            if not r["pool_address"]:
                skip_no_pool += 1
            else:
                fc, fts = _last_close(conn, r["pool_address"], ts)
                if fc is None:
                    skip_no_candles += 1
        conn.execute(
            "UPDATE stoploss_results SET status=?, final_close_usd=?, final_close_ts=? "
            "WHERE call_id=?",
            (new_status, fc, fts, r["call_id"]))
        changed += 1

    # ── trailing_results ────────────────────────────────────────────────
    held_t = conn.execute("""
        SELECT tr.call_id, cal.score_state FROM trailing_results tr
        JOIN calls cal ON cal.id = tr.call_id
        WHERE tr.status='loss' AND tr.hit_trailing_stop=0
          AND cal.status IN ('win','loss')""").fetchall()
    for r in held_t:
        new_status = "pending" if r["score_state"] == "live" else "expired"
        conn.execute("UPDATE trailing_results SET status=? WHERE call_id=?",
                     (new_status, r["call_id"]))
        changed += 1

    conn.commit()
    print(f"converted {changed} held-loss rows "
          f"(no mark: {skip_no_pool} poolless, {skip_no_candles} uncached)")
    print("stoploss now:", [dict(x) for x in conn.execute(
        "SELECT status, COUNT(*) n FROM stoploss_results GROUP BY 1")])
    print("trailing now:", [dict(x) for x in conn.execute(
        "SELECT status, COUNT(*) n FROM trailing_results GROUP BY 1")])


if __name__ == "__main__":
    main()
