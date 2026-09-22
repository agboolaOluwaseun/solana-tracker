"""
Boot-refresh gate — user policy (2026-09-22): the auto-update that fires
when the webpage opens runs ONCE PER DAY. Later opens within 24h of a
COMPLETED run skip it entirely (no Telegram scans, no GT rescore churn).

The manual "Refresh All" button always runs: the frontend sends
{force: true}, which bypasses the gate (it never marks the daily boot
stamp as done either way — a manual refresh doesn't consume the day's
boot refresh; boot will still run on the next page open if its clock is
due, at which point every channel just answers 'Already up to date'
instantly via last_scanned_at).

Resumability requirement (user's second ask): if a boot refresh is
interrupted (app closed mid-run, crash), the NEXT boot attempt continues
from the channels that never finished. Design:
  * boot_refresh runs in the unified DB (a state table + a done list), so
    progress survives reloads AND uvicorn restarts (unlike in-memory).
  * started_at doubles as the run token. finished_at NULL = run in
    flight (or interrupted by a page close / crash). The next boot after
    the join window RESUMES it: same token, completed list intact, only
    the unclosed channels are scanned.
  * an unfinished run that STARTED less than BOOT_JOIN_WINDOW ago is
    treated as genuinely in flight (double tab, StrictMode re-mount, a
    refresh another window is watching) — the boot simply skips rather
    than launching a second scan of the same run.
  * completed_channel rows are cleared ONLY when a NEW run starts after
    the previous one truly FINISHED (finished_at set). A resumed run
    keeps them, so the skip set survives exactly as long as it needs
    to and never longer.

Everything degrades open: on any DB error, treat as due so the app keeps
behaving like it did before this gate existed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from db import get_connection

log = logging.getLogger(__name__)

BOOT_REFRESH_INTERVAL = timedelta(hours=24)   # the "once a day" window
# Unfinished run younger than this is treated as genuinely in flight
# (second tab, StrictMode re-mount, another window watching the stream):
# a boot arriving then skips instead of resuming a live run.
BOOT_JOIN_WINDOW = timedelta(minutes=3)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    # Microseconds: run tokens must be unique even when a new run starts in
    # the same wall-clock second the previous one finished (test caught it).
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(raw) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def ensure_gate_table() -> None:
    """Create the gate tables if absent (called by the API layer, never
    assumed by readers)."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS boot_refresh_gate (
                id                INTEGER PRIMARY KEY CHECK (id = 1),
                started_at        TEXT NOT NULL,      -- run token (UTC ISO)
                finished_at       TEXT,               -- NULL = in flight
                rescore_at        TEXT                -- last full rescore
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS boot_refresh_channels (
                started_at    TEXT NOT NULL,           -- FK-ish: the run token
                channel_id    INTEGER NOT NULL,
                done_at       TEXT NOT NULL,
                PRIMARY KEY (started_at, channel_id)
            )""")
        conn.commit()


def _row():
    with get_connection() as conn:
        return conn.execute(
            "SELECT started_at, finished_at, rescore_at FROM boot_refresh_gate WHERE id=1"
        ).fetchone()


def boot_refresh_due() -> tuple[bool, str | None]:
    """(due?, run_token).

    due = no run ever, OR the last run finished >= 24h ago, OR the last
    run never finished and started longer than BOOT_JOIN_WINDOW ago
    (interrupted mid-way -> next boot RESUMES it with the same token and
    its completed-channel list). An unfinished run still inside the join
    window is live -> not due (skip; don't double-scan)."""
    try:
        ensure_gate_table()
        row = _row()
        if row is None:
            return True, None
        started = _parse(row["started_at"])
        finished = _parse(row["finished_at"])
        if finished is None:
            if started is None or (_now() - started) >= BOOT_JOIN_WINDOW:
                return True, row["started_at"]          # resume
            return False, row["started_at"]             # still running
        return (_now() - finished) >= BOOT_REFRESH_INTERVAL, row["started_at"]
    except Exception:  # pragma: no cover — degrade open
        log.exception("boot_refresh_due failed — assuming due")
        return True, None


def mark_boot_started() -> str | None:
    """Begin/continue the daily run. Returns the run token to use, or None
    when a live run is in flight (caller should defer to it, not abort).

    Fresh-after-finished: clears the per-channel done list and stamps a new
    token. Crashed-lease-expired: returns the OLD token and KEEPS the done
    list (that's the resume). Live run: returns the live token."""
    due, token = boot_refresh_due()
    if not due:
        return None
    try:
        row = _row()
        with get_connection() as conn:
            if row is not None and row["finished_at"] is None:
                # resume a crashed run — same token, keep completed channels
                return row["started_at"]
            new_token = _iso(_now())
            conn.execute("DELETE FROM boot_refresh_channels")
            conn.execute("""
                INSERT INTO boot_refresh_gate (id, started_at, finished_at, rescore_at)
                VALUES (1, ?, NULL, COALESCE((SELECT rescore_at FROM boot_refresh_gate WHERE id=1), NULL))
                ON CONFLICT(id) DO UPDATE SET started_at=excluded.started_at,
                                              finished_at=NULL""",
                (new_token,))
            conn.commit()
        return new_token
    except Exception:  # pragma: no cover
        log.exception("mark_boot_started failed")
        return None


def mark_channel_done(token: str | None, channel_id: int) -> None:
    if not token:
        return
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO boot_refresh_channels (started_at, channel_id, done_at) VALUES (?,?,?)",
                (token, channel_id, _iso(_now())))
            conn.commit()
    except Exception:  # pragma: no cover — bookkeeping must never break a stream
        log.exception("mark_channel_done failed")


def completed_channel_ids(token: str | None) -> set[int]:
    if not token:
        return set()
    try:
        with get_connection() as conn:
            return {r["channel_id"] for r in conn.execute(
                "SELECT channel_id FROM boot_refresh_channels WHERE started_at=?", (token,))}
    except Exception:  # pragma: no cover
        return set()


def mark_boot_finished(token: str | None, clean: bool = True) -> None:
    """Close the run (starts the 24h clock) — only when everything
    succeeded. With errors, finished_at stays NULL so the NEXT boot
    resumes the same run and retries just the failed channels (their
    done-ids were never recorded). Only the current token can close it —
    a late-finishing stale run never hijacks the stamp."""
    if not token or not clean:
        return
    try:
        with get_connection() as conn:
            conn.execute(
                "UPDATE boot_refresh_gate SET finished_at=? WHERE id=1 AND started_at=?",
                (_iso(_now()), token))
            conn.commit()
    except Exception:  # pragma: no cover
        log.exception("mark_boot_finished failed")


def hours_since_last_finished() -> float | None:
    """For the skip message: hours since the last COMPLETED run (None if
    none ever completed)."""
    try:
        row = _row()
        f = _parse(row["finished_at"]) if row else None
        return None if f is None else (_now() - f).total_seconds() / 3600.0
    except Exception:  # pragma: no cover
        return None
