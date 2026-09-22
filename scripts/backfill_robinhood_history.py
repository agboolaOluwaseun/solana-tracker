"""Robinhood historical backfill (Workstream G).

Refetches + reprices Robinhood Chain history for every kolfi.db channel that
already carries robinhood calls, anchored to the STANDARD preset window
(preset_window('5m') -> first day of the month 5 months back = 2026-04-01
when run in Sep 2026; tracks the month automatically). Solana rows are left
exactly as-is: run_backfill dedups per chain and the 7d dispatcher prices
each call with its own chain — no Solana re-pricing happens here.

NEVER touches solana_tracker.db (runtime DB is kolfi.db via .env).

Cost estimate prints BEFORE any fetching; the run requires explicit --yes.

Run:
    .venv/bin/python scripts/backfill_robinhood_history.py          # dry run (estimate only)
    .venv/bin/python scripts/backfill_robinhood_history.py --yes    # actually run
    .venv/bin/python scripts/backfill_robinhood_history.py --yes --only 666calls
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import preset_window, run_backfill, Progress  # noqa: E402
from db import get_connection  # noqa: E402
import config  # noqa: E402

# GeckoTerminal 5 RPM measured ceiling (see rate-limiter analysis): ~1.6
# requests per call in the steady 7d engine path (1 fetch + ~0.6 DS resolve).
REQ_PER_CALL = 1.6
RPM = 5.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--yes", action="store_true",
                    help="actually run the backfill (without it: estimate only)")
    ap.add_argument("--only", metavar="USERNAME_PART",
                    help="only channels whose username contains this substring")
    args = ap.parse_args()

    conn = get_connection()
    q = """
        SELECT c.id, c.username, c.telegram_channel_id, c.title,
               COUNT(rh.call_id) AS rh_calls
        FROM channels c
        JOIN (SELECT channel_id AS call_id FROM calls WHERE chain='robinhood') rh
          ON rh.call_id = c.id
        GROUP BY c.id
        ORDER BY rh_calls DESC
    """
    rows = conn.execute(q).fetchall()
    if args.only:
        rows = [r for r in rows if args.only.lower() in (r["username"] or "").lower()]
    if not rows:
        print("no robinhood channels found in kolfi.db")
        return 1

    window_start, window_end = preset_window("5m")
    print(f"window: {window_start:%Y-%m-%d} .. {window_end:%Y-%m-%d %H:%M} UTC")
    print(f"DB:     {Path(config.settings.db_path).name} "
          f"(schema: {config.settings.schema_file})")
    print(f"engine: {config.settings.pricing_engine}")
    total_calls = sum(r["rh_calls"] for r in rows)
    est_minutes = total_calls * REQ_PER_CALL / RPM
    print(f"\nchannels with robinhood history: {len(rows)}")
    for r in rows:
        print(f"  {r['title']:32s} @{r['username'] or '—':24s} {r['rh_calls']:3d} RH calls")
    print(f"existing RH calls: {total_calls}  (wider window may surface more)")
    print(f"cost estimate: {total_calls:.0f} calls x ~{REQ_PER_CALL} reqs "
          f"= {total_calls * REQ_PER_CALL:.0f} reqs at {RPM:.0f} RPM "
          f"~ {est_minutes:.0f} min")

    if not args.yes:
        print("\nDRY RUN — nothing fetched. Re-run with --yes to proceed.")
        return 0

    ok = 0
    failed = []
    for r in rows:
        ref = f"@{r['username']}" if r["username"] else str(r["telegram_channel_id"])
        print(f"\n=== {r['title']} ({ref}) ===", flush=True)

        def cb(p: Progress, _t=[datetime.now()], _n=[0]):
            # Real counters only — no fake percentages. Throttle to ~2/s.
            if (datetime.now() - _t[0]).total_seconds() < 0.5 and p.stage != "done":
                return
            _t[0] = datetime.now()
            _n[0] += 1
            print(f"  [{p.stage:6s}] scanned={p.scanned} found={p.found} "
                  f"priced={p.priced} unpriceable={p.unpriceable} {p.message}",
                  flush=True)

        try:
            prog = run_backfill(ref, window_start, window_end,
                                title=r["title"], username=r["username"],
                                progress_cb=cb,
                                birdeye_rescue=True)  # initial history scan
            print(f"  done: priced={prog.priced} unpriceable={prog.unpriceable}")
            ok += 1
        except Exception as e:
            print(f"  FAILED: {e}")
            failed.append(r["title"])

    print(f"\ncompleted {ok}/{len(rows)} channels"
          + (f"; failed: {', '.join(failed)}" if failed else ""))
    dec = conn.execute(
        "SELECT SUM(is_win) w, COUNT(*) n FROM calls WHERE chain='robinhood' "
        "AND status IN ('win','loss')").fetchone()
    if dec["n"]:
        print(f"robinhood all-time now: {dec['w']}W/{dec['n']} "
              f"({dec['w'] / dec['n'] * 100:.1f}% plain)")
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
