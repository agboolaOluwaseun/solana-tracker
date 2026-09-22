"""
CLI entry point for a manual backfill.

Usage:
    python -m scripts.backfill --channel @some_channel --start 2026-03-15
    python -m scripts.backfill --channel @some_channel --start 2026-03-15 --limit 5000

Run from the project root (solana_tracker/) so imports resolve.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Ensure the package root is importable when run as a module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings                              # noqa: E402
from pipeline import Progress, run_backfill              # noqa: E402


def _parse_start(s: str) -> datetime:
    """Parse a YYYY-MM-DD date as naive UTC midnight."""
    dt = datetime.strptime(s, "%Y-%m-%d")
    return dt


def _print_progress(p: Progress) -> None:
    if p.stage == "fetch":
        sys.stdout.write(f"\r[fetch] scanned={p.scanned}      ")
    elif p.stage == "parse":
        sys.stdout.write(f"\r[parse] calls found={p.found}      ")
    elif p.stage == "price":
        sys.stdout.write(
            f"\r[price] {p.scanned}/{p.total_calls}  "
            f"priced={p.priced} unpriceable={p.unpriceable}      "
        )
    elif p.stage == "done":
        sys.stdout.write(
            f"\n[done] found={p.found} priced={p.priced} "
            f"unpriceable={p.unpriceable}\n"
        )
    elif p.stage == "error":
        sys.stdout.write(f"\n[error] {p.message}\n")
    sys.stdout.flush()


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a solana_tracker backfill.")
    ap.add_argument("--channel", required=True, help="Telegram @username or id")
    ap.add_argument(
        "--start", required=True,
        help="Window start date YYYY-MM-DD (UTC). 12 weeks forward from here.",
    )
    ap.add_argument("--limit", type=int, default=None, help="Cap on messages scanned")
    ap.add_argument("--weeks", type=int, default=None, help="Override window weeks")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.weeks:
        # Mutate the frozen settings via env re-read isn't trivial; rely on the
        # configured default and document that --weeks only adjusts the window_end.
        end = _parse_start(args.start) + timedelta(weeks=args.weeks)
    else:
        end = _parse_start(args.start) + timedelta(weeks=settings.window_weeks)

    username = args.channel.lstrip("@") if args.channel.startswith("@") else None
    start_dt = _parse_start(args.start)

    print(f"Backfilling {args.channel}: {start_dt.date()} -> {end.date()} "
          f"({settings.window_weeks} weeks)")
    prog = run_backfill(
        channel_ref=args.channel,
        window_start=start_dt,
        window_end=end,
        username=username,
        fetch_limit=args.limit,
        progress_cb=_print_progress,
        birdeye_rescue=True,  # initial scan: rescue allowed
    )
    return 0 if prog.stage != "error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
