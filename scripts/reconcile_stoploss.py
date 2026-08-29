"""
Reconcile missing stoploss_results for calls that have normal results.

Finds all calls with status in ('win', 'loss') but no corresponding row in
stoploss_results, then re-scores them using cached candles and inserts the
missing stoploss results.

No API calls — uses the same cache-first backtest logic.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from db import get_connection
from pricing.backtest import score_candles, score_candles_stoploss
from models import StoplossResult

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def reconcile_stoploss_results(dry_run: bool = True) -> dict:
    """
    Find calls with normal results but no stoploss_results, and backfill them.

    Returns a summary dict with counts.
    """
    conn = get_connection()

    # Find calls with normal results but no stoploss_results
    missing = conn.execute("""
        SELECT c.id, c.channel_id, c.token_address, c.call_timestamp, c.status
        FROM calls c
        WHERE c.status IN ('win', 'loss')
        AND c.id NOT IN (SELECT call_id FROM stoploss_results)
        ORDER BY c.call_timestamp ASC
    """).fetchall()

    log.info("Found %d calls with missing stoploss_results", len(missing))

    if not missing:
        return {"missing": 0, "backfilled": 0, "errors": 0}

    from pricing.cache import load_candles
    from config import settings

    backfilled = 0
    errors = 0

    for row in missing:
        call_id = row["id"]
        token_address = row["token_address"]
        call_ts = datetime.fromisoformat(row["call_timestamp"].replace("Z", ""))

        log.info("Re-scoring call %d (%s) for channel %d", call_id, token_address[:12] + "...", row["channel_id"])

        try:
            # Load cached candles for the 12h window (no API calls)
            start_ts = call_ts - timedelta(minutes=5)
            end_ts = start_ts + timedelta(hours=settings.peak_window_hours)
            
            # Try to load from cache (GeckoTerminal or Birdeye)
            candles = load_candles("geckoterminal", token_address, start_ts, end_ts)
            if not candles:
                candles = load_candles("birdeye", token_address, start_ts, end_ts)
            
            if not candles:
                log.warning("No cached candles for call %d, skipping", call_id)
                errors += 1
                continue

            # Score using cached candles (both strategies)
            result = score_candles(candles, call_ts, token_address)
            sl_result = score_candles_stoploss(candles, call_ts, token_address)

            if not dry_run:
                # Insert the missing stoploss_result
                conn.execute("""
                    INSERT INTO stoploss_results
                        (call_id, entry_price_usd, peak_price_usd, peak_timestamp,
                         peak_profit_pct, hit_stoploss, stoploss_timestamp, is_win, status, error, computed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(call_id) DO UPDATE SET
                        entry_price_usd=excluded.entry_price_usd,
                        peak_price_usd=excluded.peak_price_usd,
                        peak_timestamp=excluded.peak_timestamp,
                        peak_profit_pct=excluded.peak_profit_pct,
                        hit_stoploss=excluded.hit_stoploss,
                        stoploss_timestamp=excluded.stoploss_timestamp,
                        is_win=excluded.is_win,
                        status=excluded.status,
                        error=excluded.error,
                        computed_at=datetime('now')
                """, (
                    call_id,
                    sl_result.entry_price_usd,
                    sl_result.peak_price_usd,
                    sl_result.peak_timestamp.isoformat() if sl_result.peak_timestamp else None,
                    sl_result.peak_profit_pct,
                    1 if sl_result.hit_stoploss else 0,
                    sl_result.stoploss_timestamp.isoformat() if sl_result.stoploss_timestamp else None,
                    1 if sl_result.is_win else 0,
                    sl_result.status,
                    sl_result.error,
                ))
                conn.commit()
                log.info("✓ Backfilled stoploss_result for call %d: %s", call_id, sl_result.status)
            else:
                log.info("[DRY RUN] Would backfill call %d: %s", call_id, sl_result.status)

            backfilled += 1
        except Exception as e:
            log.exception("Failed to backfill call %d", call_id)
            errors += 1

    return {
        "missing": len(missing),
        "backfilled": backfilled,
        "errors": errors,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Reconcile missing stoploss_results")
    parser.add_argument("--execute", action="store_true", help="Actually write to DB (default is dry run)")
    args = parser.parse_args()

    summary = reconcile_stoploss_results(dry_run=not args.execute)
    print("\nSummary:")
    for k, v in summary.items():
        print(f"  {k}: {v}")
