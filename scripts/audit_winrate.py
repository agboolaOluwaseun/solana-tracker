"""
Focused win-rate audit.

Pulls every priced call from the DB and shows, per call:
  - the token + call timestamp
  - the entry price (first candle open at/after call time)
  - the peak price + peak profit % (current logic)
  - a peak profit % computed from the CLOSE of each candle too (sanity check)

This lets us see whether the low win rate is a detection bug (e.g. picking the
wrong pool, wrong entry candle, hourly candles missing intrahour spikes) vs.
genuinely low-performing tokens.

Run:  .venv/bin/python scripts/audit_winrate.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import get_connection
from pricing.geckoterminal import GeckoTerminalClient
from pricing.cache import load_candles


def main() -> int:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT token_address, token_symbol, call_timestamp, status,
               pool_address, entry_price_usd, peak_price_usd, peak_profit_pct
        FROM calls
        ORDER BY call_timestamp ASC
        """
    ).fetchall()

    print(f"\n=== AUDIT: {len(rows)} calls ===\n")
    print(f"{'symbol':12s} {'call_time':20s} {'status':18s} {'entry':>12s} {'peak':>12s} {'profit%':>8s}")
    print("-" * 90)

    wins = losses = unpriceable = 0
    suspicious = []  # calls where cached candles show a much higher peak than recorded

    for r in rows:
        sym = (r["token_symbol"] or "?")[:11]
        ts = r["call_timestamp"][:19]
        status = r["status"]
        entry = r["entry_price_usd"]
        peak = r["peak_price_usd"]
        pct = r["peak_profit_pct"]

        entry_s = f"{entry:.8f}" if entry is not None else "—"
        peak_s = f"{peak:.8f}" if peak is not None else "—"
        pct_s = f"+{pct:.1f}" if pct is not None else "—"

        print(f"{sym:12s} {ts:20s} {status:18s} {entry_s:>12s} {peak_s:>12s} {pct_s:>8s}")

        if status == "win":
            wins += 1
        elif status == "loss":
            losses += 1
        else:
            unpriceable += 1

        # For priced calls, re-check the cached candles independently.
        if r["pool_address"] and entry:
            call_ts = datetime.fromisoformat(r["call_timestamp"].replace("Z", ""))
            end_ts = call_ts + timedelta(hours=24)
            candles = load_candles(r["pool_address"], r["token_address"], call_ts, end_ts)
            if candles:
                # What's the actual max high in the cached candles?
                real_peak = max(c.high for c in candles)
                real_pct = (real_peak / entry - 1) * 100 if entry else 0
                # And the max close (in case high candles are sparse/noisy)?
                real_peak_close = max(c.close for c in candles)
                close_pct = (real_peak_close / entry - 1) * 100 if entry else 0
                # If our recorded peak differs materially from the cached max,
                # flag it.
                if r["peak_price_usd"] and real_peak > r["peak_price_usd"] * 1.05:
                    suspicious.append({
                        "symbol": sym, "call_time": ts,
                        "recorded_peak": r["peak_price_usd"],
                        "cached_max_high": real_peak, "cached_max_high_pct": real_pct,
                        "cached_max_close_pct": close_pct,
                        "n_candles": len(candles),
                    })

    print("-" * 90)
    total = wins + losses + unpriceable
    print(f"\nWins: {wins}  Losses: {losses}  Unpriceable: {unpriceable}  Total: {total}")
    if total:
        print(f"Recorded win rate: {wins/total*100:.1f}%")

    # Now: re-fetch fresh data for a few LOSS calls to see if the entry/peak
    # logic is wrong (vs. just stale cache). Pick the 3 biggest "almost-wins".
    print("\n=== DEEP-DIVE: re-fetching fresh candles for top 'near-miss' losses ===\n")
    near_misses = conn.execute(
        """
        SELECT token_address, token_symbol, call_timestamp, pool_address,
               entry_price_usd, peak_profit_pct
        FROM calls
        WHERE status='loss' AND peak_profit_pct IS NOT NULL
        ORDER BY peak_profit_pct DESC
        LIMIT 3
        """
    ).fetchall()

    client = GeckoTerminalClient()
    for r in near_misses:
        sym = r["token_symbol"] or "?"
        call_ts = datetime.fromisoformat(r["call_timestamp"].replace("Z", ""))
        end_ts = call_ts + timedelta(hours=24)
        print(f"--- {sym} called {call_ts} (recorded +{r['peak_profit_pct']:.1f}%) ---")
        # Bypass cache by reading fresh from API
        try:
            candles = client.fetch_ohlcv(
                r["pool_address"], r["token_address"], call_ts, end_ts, aggregate="1"
            )
        except Exception as e:
            print(f"  fetch error: {e}")
            continue
        if not candles:
            print("  no candles (even fresh)")
            continue
        print(f"  candles: {len(candles)} | first candle ts={candles[0].timestamp} open={candles[0].open:.8f}")
        # Entry = open of first candle at/after call (current logic)
        entry_open = candles[0].open
        # Alt entry: close of the candle CONTAINING the call time (more precise)
        entry_close = None
        for c in candles:
            if c.timestamp <= call_ts < c.timestamp + timedelta(hours=1):
                entry_close = c.close
                break
        max_high = max(c.high for c in candles)
        max_close = max(c.close for c in candles)
        print(f"  entry (first candle open): {entry_open:.8f}")
        if entry_close:
            print(f"  entry (call-candle close): {entry_close:.8f}")
        print(f"  max HIGH:  {max_high:.8f}  -> +{(max_high/entry_open-1)*100:.1f}% from first-open")
        if entry_close:
            print(f"  max HIGH:  {max_high:.8f}  -> +{(max_high/entry_close-1)*100:.1f}% from call-close")
        print(f"  max CLOSE: {max_close:.8f} -> +{(max_close/entry_open-1)*100:.1f}% from first-open")
        # Would either metric make this a 2x win?
        win_open = max_high >= 2 * entry_open
        win_close = entry_close and max_high >= 2 * entry_close
        print(f"  2x win (from first-open)?  {'YES' if win_open else 'no'}")
        print(f"  2x win (from call-close)?  {'YES' if win_close else 'no'}")
        print()

    if suspicious:
        print(f"\n=== {len(suspicious)} SUSPICIOUS calls (cached peak > recorded peak) ===")
        for s in suspicious:
            print(f"  {s['symbol']} @ {s['call_time']}: recorded peak ${s['recorded_peak']:.8f} "
                  f"but cached max high ${s['cached_max_high']:.8f} (+{s['cached_max_high_pct']:.1f}%)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
