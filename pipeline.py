"""
Backfill pipeline orchestrator.

Runs the full fetch -> parse -> price -> score -> persist flow for one channel
over a fixed window. Designed to be RESUMABLE: every call is committed as soon
as it's priced, so a crash (or a Ctrl-C, or a 429 storm that exhausts retries)
lets the next run pick up exactly where it left off — only `pending` calls are
priced.

Progress is reported via an optional callback so the UI can stream it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from config import settings
from db import get_connection, init_db, transaction
from ingestion.address_parser import deduplicate_calls, parse_message
from models import WeekBuckets
from pricing.backtest import backtest_call, upsert_token_meta
from pricing.geckoterminal import GeckoTerminalClient

log = logging.getLogger(__name__)


def _import_fetch_window_sync():
    """Lazy import — telethon_fetcher requires the telethon package which
    may not be installed for UI-only usage (investigation, manage, etc)."""
    from ingestion.telethon_fetcher import fetch_window_sync
    return fetch_window_sync


@dataclass
class Progress:
    stage: str            # 'fetch' | 'parse' | 'price' | 'done' | 'error'
    scanned: int = 0
    found: int = 0
    priced: int = 0
    unpriceable: int = 0
    total_calls: int = 0
    message: str = ""


ProgressCb = Callable[[Progress], None]


def _noop(_p: Progress) -> None:
    pass


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat() + "Z"


def make_buckets(start: datetime) -> WeekBuckets:
    """Build the 12 fixed weekly buckets from a naive UTC start datetime."""
    end = start + timedelta(weeks=settings.window_weeks)
    week_starts = [start + timedelta(weeks=i) for i in range(settings.window_weeks)]
    return WeekBuckets(start=start, end=end, week_starts=week_starts)


def ensure_channel(
    channel_ref: str,
    telegram_channel_id: int,
    title: Optional[str],
    username: Optional[str],
    window_start: datetime,
    window_end: datetime,
) -> int:
    """Insert/return the channel row for a backfill."""
    with transaction() as conn:
        row = conn.execute(
            "SELECT id FROM channels WHERE telegram_channel_id = ?",
            (telegram_channel_id,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE channels SET title=?, username=?, window_start=?, window_end=? WHERE id=?",
                (title, username, _iso(window_start), _iso(window_end), row["id"]),
            )
            return row["id"]
        cur = conn.execute(
            """
            INSERT INTO channels (telegram_channel_id, username, title, window_start, window_end)
            VALUES (?, ?, ?, ?, ?)
            """,
            (telegram_channel_id, username, title, _iso(window_start), _iso(window_end)),
        )
        return cur.lastrowid


def persist_parsed_call(channel_id: int, call, week_index: int) -> None:
    """Insert a parsed call as 'pending' (idempotent on the unique key)."""
    with transaction() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO calls
                (channel_id, message_id, raw_text, token_address, token_symbol,
                 token_name, call_timestamp, week_index, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                channel_id,
                call.message_id,
                call.raw_text[:2000],
                call.token_address,
                call.token_symbol,
                call.token_name,
                _iso(call.timestamp),
                week_index,
            ),
        )


def apply_backtest(channel_id: int, call_id: int, message_id: int, result) -> None:
    """Write a priced result back to the calls row."""
    with transaction() as conn:
        conn.execute(
            """
            UPDATE calls SET
                pool_address = ?,
                entry_price_usd = ?,
                peak_price_usd = ?,
                peak_timestamp = ?,
                peak_profit_pct = ?,
                is_win = ?,
                status = ?,
                priced_at = datetime('now')
            WHERE id = ?
            """,
            (
                result.pool_address,
                result.entry_price_usd,
                result.peak_price_usd,
                _iso(result.peak_timestamp) if result.peak_timestamp else None,
                result.peak_profit_pct,
                1 if result.is_win else 0,
                result.status,
                call_id,
            ),
        )


def run_backfill(
    channel_ref: str,
    window_start: datetime,
    window_end: datetime,
    title: Optional[str] = None,
    username: Optional[str] = None,
    fetch_limit: Optional[int] = None,
    progress_cb: ProgressCb = _noop,
) -> Progress:
    """
    Execute the full backfill for one channel/window. Resumable.

    channel_ref: the value passed to Telethon (e.g. '@some_channel').
    window_start/window_end: naive UTC datetimes.
    """
    init_db()
    progress = Progress(stage="fetch")
    progress_cb(progress)

    buckets = make_buckets(window_start)

    # 1. FETCH ---------------------------------------------------------------
    fetched = []
    try:
        def on_scan(n):
            progress.scanned = n
            progress_cb(progress)

        fetch_window_sync = _import_fetch_window_sync()
        fetched = fetch_window_sync(
            channel_ref,
            window_start,
            window_end,
            limit=fetch_limit,
            progress_cb=on_scan,
        )
    except Exception as e:
        log.exception("fetch failed for %s", channel_ref)
        progress.stage = "error"
        progress.message = f"fetch failed: {e}"
        progress_cb(progress)
        return progress

    # fetch_window_sync returns (messages, channel_title)
    if isinstance(fetched, tuple) and len(fetched) == 2:
        fetched, fetched_title = fetched
    else:
        fetched_title = None
    # Resolve channel id from fetched messages (all share the same source id).
    tg_id = fetched[0].channel_id if fetched else abs(hash(channel_ref)) % (10 ** 12)
    channel_id = ensure_channel(channel_ref, tg_id, title or fetched_title, username, window_start, window_end)

    # 2. PARSE → deduplicate → persist -----------------------------------
    progress.stage = "parse"
    all_parsed: list = []
    for msg in fetched:
        parsed = parse_message(msg)
        if parsed is not None:
            all_parsed.append(parsed)
    raw_count = len(all_parsed)
    # Sort chronologically (oldest first) BEFORE dedup so the FIRST call
    # (not the latest update/pump post) is the one we keep.
    all_parsed.sort(key=lambda c: c.timestamp)
    # Deduplicate: same token_address or ticker within 24h → keep first only.
    all_parsed = deduplicate_calls(all_parsed)
    deduped_count = len(all_parsed)
    for parsed in all_parsed:
        week_index = buckets.week_index(parsed.timestamp)
        if week_index == 0:
            week_index = max(1, min(settings.window_weeks, week_index or 1))
        persist_parsed_call(channel_id, parsed, week_index)
    progress.found = deduped_count
    progress.total_calls = deduped_count
    progress_cb(progress)
    if raw_count != deduped_count:
        log.info(
            "dedup: %d raw calls -> %d after window-wide dedup (%d pump-updates removed)",
            raw_count, deduped_count, raw_count - deduped_count,
        )

    # 3. PRICE all pending calls for this channel ---------------------------
    progress.stage = "price"
    conn = get_connection()
    pending = conn.execute(
        "SELECT id, message_id, token_address, call_timestamp FROM calls "
        "WHERE channel_id = ? AND status = 'pending' ORDER BY call_timestamp ASC",
        (channel_id,),
    ).fetchall()
    progress.total_calls = len(pending)
    log.info(
        "pricing %d pending calls for channel %s "
        "(tokens discovered: %s)",
        len(pending), channel_ref,
        ", ".join(row["token_address"][:8] + "…" for row in pending[:5])
        + (f" (+{len(pending)-5} more)" if len(pending) > 5 else ""),
    )
    progress_cb(progress)

    client = GeckoTerminalClient()
    priced = 0
    unpriceable = 0
    for i, row in enumerate(pending, start=1):
        addr = row["token_address"]
        sym = ""  # we don't have it in the pending row; use the address prefix
        log.info(
            "[%d/%d] requesting data for %s…",
            i, len(pending), addr[:12] + "…" + addr[-4:],
        )
        call_ts = datetime.fromisoformat(row["call_timestamp"].replace("Z", ""))
        try:
            result = backtest_call(client, addr, call_ts)
        except Exception as e:
            log.exception("backtest failed for call %s", row["id"])
            # Treat unexpected errors as unpriceable losses (don't block the run).
            from models import BacktestResult
            result = BacktestResult(
                entry_price_usd=None, peak_price_usd=None, peak_timestamp=None,
                peak_profit_pct=None, is_win=False, status="unpriceable_loss",
                error=str(e),
            )
        apply_backtest(channel_id, row["id"], row["message_id"], result)

        if result.status == "unpriceable_loss":
            unpriceable += 1
            # Log unpriceable tokens to the terminal so you can investigate them.
            log.warning(
                "[UNPRICEABLE] %s | %s | %s",
                row["call_timestamp"],
                row.get("token_symbol") or "?",
                addr,
            )
        else:
            priced += 1
        progress.priced = priced
        progress.unpriceable = unpriceable
        progress.scanned = i
        progress_cb(progress)

    # 4. Record run ----------------------------------------------------------
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO ingestion_runs
                (channel_id, started_at, finished_at, mode, messages_scanned,
                 calls_found, calls_priced, calls_unpriceable, status)
            VALUES (?, ?, datetime('now'), 'resume', ?, ?, ?, ?, 'completed')
            """,
            (
                channel_id,
                _iso(datetime.now(timezone.utc).replace(tzinfo=None)),
                progress.scanned,
                progress.found,
                priced,
                unpriceable,
            ),
        )

    progress.stage = "done"
    progress.message = "backfill complete"
    progress_cb(progress)
    return progress


def reprice_calls(
    channel_id: int,
    statuses: list[str] | None = None,
    progress_cb: ProgressCb = _noop,
) -> Progress:
    """
    Re-price calls for a channel that are in one of the given statuses.

    Defaults to re-pricing both 'pending' and 'unpriceable_loss' calls —
    useful when a backfill was interrupted (pending) or when transient API
    errors marked tokens as unpriceable.

    Does NOT re-fetch messages from Telegram — only re-prices existing rows.
    """
    if statuses is None:
        statuses = ["pending", "unpriceable_loss"]

    init_db()
    progress = Progress(stage="price")
    progress_cb(progress)

    placeholders = ",".join("?" * len(statuses))
    conn = get_connection()
    rows = conn.execute(
        f"SELECT id, message_id, token_address, token_symbol, call_timestamp "
        f"FROM calls WHERE channel_id = ? AND status IN ({placeholders}) "
        f"ORDER BY call_timestamp ASC",
        [channel_id, *statuses],
    ).fetchall()

    progress.total_calls = len(rows)
    progress_cb(progress)
    if not rows:
        progress.stage = "done"
        progress.message = "no calls to re-price"
        progress_cb(progress)
        return progress

    log.info("re-pricing %d calls for channel %d (statuses: %s)", len(rows), channel_id, statuses)

    client = GeckoTerminalClient()
    priced = 0
    unpriceable = 0

    for i, row in enumerate(rows, start=1):
        addr = row["token_address"]
        call_ts = datetime.fromisoformat(row["call_timestamp"].replace("Z", ""))
        try:
            result = backtest_call(client, addr, call_ts)
        except Exception as e:
            log.exception("reprice backtest failed for call %s", row["id"])
            from models import BacktestResult
            result = BacktestResult(
                entry_price_usd=None, peak_price_usd=None, peak_timestamp=None,
                peak_profit_pct=None, is_win=False, status="unpriceable_loss",
                error=str(e),
            )
        apply_backtest(channel_id, row["id"], row["message_id"], result)

        if result.status == "unpriceable_loss":
            unpriceable += 1
            log.warning(
                "[UNPRICEABLE] %s | %s | %s",
                row["call_timestamp"],
                row.get("token_symbol") or "?",
                addr,
            )
        else:
            priced += 1
        progress.priced = priced
        progress.unpriceable = unpriceable
        progress.scanned = i
        progress_cb(progress)

    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO ingestion_runs
                (channel_id, started_at, finished_at, mode, messages_scanned,
                 calls_found, calls_priced, calls_unpriceable, status)
            VALUES (?, ?, datetime('now'), 'reprice', 0, ?, ?, ?, 'completed')
            """,
            (
                channel_id,
                _iso(datetime.now(timezone.utc).replace(tzinfo=None)),
                len(rows), priced, unpriceable,
            ),
        )

    progress.stage = "done"
    progress.message = f"re-priced {priced} calls ({unpriceable} still unpriceable)"
    progress_cb(progress)
    return progress


def delete_channel_data(channel_id: int, delete_channel: bool = False) -> dict:
    """
    Delete all calls and ingestion runs for a channel.

    If delete_channel=True, also removes the channel row itself.
    Returns a summary dict with deletion counts.
    """
    init_db()
    summary = {"calls_deleted": 0, "runs_deleted": 0, "channel_deleted": False}

    with transaction() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM calls WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        summary["calls_deleted"] = count["n"] if count else 0
        conn.execute("DELETE FROM calls WHERE channel_id = ?", (channel_id,))

        count = conn.execute(
            "SELECT COUNT(*) AS n FROM ingestion_runs WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        summary["runs_deleted"] = count["n"] if count else 0
        conn.execute("DELETE FROM ingestion_runs WHERE channel_id = ?", (channel_id,))

        if delete_channel:
            conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
            summary["channel_deleted"] = True

    log.info(
        "deleted channel %d data: %d calls, %d runs, channel=%s",
        channel_id, summary["calls_deleted"], summary["runs_deleted"], summary["channel_deleted"],
    )
    return summary


def delete_calls_by_status(channel_id: int, statuses: list[str]) -> dict:
    """
    Delete calls for a channel that match specific statuses.

    Useful for clearing only losses, pending, or unpriceable calls while keeping wins.
    Returns a summary dict with deletion count.
    """
    init_db()
    summary = {"calls_deleted": 0}

    if not statuses:
        return summary

    placeholders = ",".join("?" * len(statuses))
    with transaction() as conn:
        count = conn.execute(
            f"SELECT COUNT(*) AS n FROM calls WHERE channel_id = ? AND status IN ({placeholders})",
            [channel_id, *statuses],
        ).fetchone()
        summary["calls_deleted"] = count["n"] if count else 0
        conn.execute(
            f"DELETE FROM calls WHERE channel_id = ? AND status IN ({placeholders})",
            [channel_id, *statuses],
        )

    log.info(
        "deleted %d calls for channel %d (statuses: %s)",
        summary["calls_deleted"], channel_id, statuses,
    )
    return summary


# ── Birdeye pricing for unpriceable calls ───────────────────────────────────

def reprice_with_birdeye(
    channel_id: int,
    call_ids: list[int] | None = None,
    progress_cb: ProgressCb = _noop,
) -> Progress:
    """
    Re-price unpriceable (or pending) calls using Birdeye's OHLCV API.

    If call_ids is None, prices ALL unpriceable_loss calls for the channel.
    If call_ids is provided, only prices those specific call rows.
    """
    from pricing.birdeye import BirdeyeClient, BirdeyeError
    from pricing.backtest import score_candles
    from models import BacktestResult

    init_db()
    progress = Progress(stage="price")
    progress_cb(progress)

    conn = get_connection()

    if call_ids:
        placeholders = ",".join("?" * len(call_ids))
        rows = conn.execute(
            f"SELECT id, message_id, token_address, call_timestamp "
            f"FROM calls WHERE id IN ({placeholders}) "
            f"ORDER BY call_timestamp ASC",
            call_ids,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, message_id, token_address, call_timestamp FROM calls "
            "WHERE channel_id = ? AND status = 'unpriceable_loss' "
            "ORDER BY call_timestamp ASC",
            (channel_id,),
        ).fetchall()

    progress.total_calls = len(rows)
    progress_cb(progress)
    if not rows:
        progress.stage = "done"
        progress.message = "no calls to re-price with Birdeye"
        progress_cb(progress)
        return progress

    client = BirdeyeClient()
    priced = 0
    unpriceable = 0

    for i, row in enumerate(rows, start=1):
        addr = row["token_address"]
        call_ts = datetime.fromisoformat(row["call_timestamp"].replace("Z", ""))
        end_ts = call_ts + timedelta(hours=settings.peak_window_hours)

        try:
            # Fetch minute candles for entry price (tight window)
            minute_candles = client.fetch_ohlcv(
                addr,
                call_ts - timedelta(minutes=5),
                call_ts + timedelta(minutes=10),
                interval="1m",
            )
            # Fetch hourly candles for 7-day peak scan
            hour_candles = client.fetch_ohlcv(
                addr,
                call_ts,
                end_ts,
                interval="1H",
            )
        except BirdeyeError as e:
            log.warning("Birdeye pricing failed for %s: %s", addr[:12] + "…", e)
            result = BacktestResult(
                entry_price_usd=None, peak_price_usd=None, peak_timestamp=None,
                peak_profit_pct=None, is_win=False, status="unpriceable_loss",
                error=f"Birdeye: {e}",
            )
            unpriceable += 1
            conn.execute(
                "UPDATE calls SET status='unpriceable_loss', priced_at=datetime('now') "
                "WHERE id = ?",
                (row["id"],),
            )
            progress.unpriceable = unpriceable
            progress.priced = priced
            progress.scanned = i
            progress_cb(progress)
            continue

        # Score using the same logic as GeckoTerminal backtest
        all_candles = list(minute_candles) + list(hour_candles)
        result = score_candles(all_candles, call_ts, addr)

        conn.execute(
            """UPDATE calls SET
                pool_address = ?,
                entry_price_usd = ?,
                peak_price_usd = ?,
                peak_timestamp = ?,
                peak_profit_pct = ?,
                is_win = ?,
                status = ?,
                priced_at = datetime('now')
            WHERE id = ?""",
            (
                result.pool_address or "birdeye",
                result.entry_price_usd,
                result.peak_price_usd,
                _iso(result.peak_timestamp) if result.peak_timestamp else None,
                result.peak_profit_pct,
                1 if result.is_win else 0,
                result.status,
                row["id"],
            ),
        )

        if result.status == "unpriceable_loss":
            unpriceable += 1
        else:
            priced += 1

        progress.priced = priced
        progress.unpriceable = unpriceable
        progress.scanned = i
        progress_cb(progress)

    # Record run
    with transaction() as c:
        c.execute(
            """INSERT INTO ingestion_runs
                (channel_id, started_at, finished_at, mode, messages_scanned,
                 calls_found, calls_priced, calls_unpriceable, status)
            VALUES (?, ?, datetime('now'), 'birdeye_reprice', 0, ?, ?, ?, 'completed')""",
            (
                channel_id,
                _iso(datetime.now(timezone.utc).replace(tzinfo=None)),
                len(rows), priced, unpriceable,
            ),
        )

    progress.stage = "done"
    progress.message = f"Birdeye re-price: {priced} priced, {unpriceable} still unpriceable"
    progress_cb(progress)
    return progress


# ── Manual status override ──────────────────────────────────────────────────

def manual_override_call(
    call_id: int,
    new_status: str,
    entry_price: Optional[float] = None,
    peak_price: Optional[float] = None,
    peak_profit_pct: Optional[float] = None,
    note: Optional[str] = None,
) -> dict:
    """
    Manually override a call's status and optional pricing fields.

    new_status must be one of: 'win', 'loss', 'pending', 'unpriceable_loss'.
    When setting win/loss, entry_price and peak_price should be provided so
    the win rate math works correctly.

    Returns a summary dict with the old and new status.
    """
    valid_statuses = {"win", "loss", "pending", "unpriceable_loss"}
    if new_status not in valid_statuses:
        raise ValueError(f"Invalid status '{new_status}'. Must be one of: {valid_statuses}")

    init_db()
    conn = get_connection()

    # Fetch current row
    row = conn.execute(
        "SELECT id, status, token_address, call_timestamp, channel_id FROM calls WHERE id = ?",
        (call_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"Call ID {call_id} not found in database")

    old_status = row["status"]
    is_win = 1 if new_status == "win" else 0

    with transaction() as c:
        c.execute(
            """UPDATE calls SET
                status = ?,
                is_win = ?,
                entry_price_usd = COALESCE(?, entry_price_usd),
                peak_price_usd = COALESCE(?, peak_price_usd),
                peak_profit_pct = COALESCE(?, peak_profit_pct),
                priced_at = datetime('now')
            WHERE id = ?""",
            (
                new_status,
                is_win,
                entry_price,
                peak_price,
                peak_profit_pct,
                call_id,
            ),
        )

    log.info(
        "Manual override: call %s (%s) %s → %s",
        call_id, row["token_address"][:10] + "…", old_status, new_status,
    )

    return {
        "call_id": call_id,
        "token_address": row["token_address"],
        "old_status": old_status,
        "new_status": new_status,
        "channel_id": row["channel_id"],
    }
