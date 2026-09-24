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

import contextlib
import logging
import threading
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

# Live rescore concurrency state (see rescore_live_calls / pause_rescoring).
_RESCORE_LOCK = threading.Lock()
_RESCORE_RUNNING = False
_RESCORE_RERUN = False
_RESCORE_PAUSE = threading.Event()
_RESCORE_PAUSE.set()  # set = allowed to run; fetch streams clear it


_PAUSE_DEPTH = 0


@contextlib.contextmanager
def pause_rescoring():
    """Ref-counted: the background rescore checks the pause between calls and
    waits while any user-initiated backfill runs, then resumes automatically.
    User fetch always outranks opportunistic refresh; a stream must never run
    its own end-of-stream rescore while paused (it runs after leaving this)."""
    global _PAUSE_DEPTH
    with _RESCORE_LOCK:
        _PAUSE_DEPTH += 1
        _RESCORE_PAUSE.clear()
    try:
        yield
    finally:
        with _RESCORE_LOCK:
            _PAUSE_DEPTH -= 1
            if _PAUSE_DEPTH == 0:
                _RESCORE_PAUSE.set()


def run_backfill_guarded(*args, **kwargs) -> Progress:
    """run_backfill wrapped so the background rescore yields to it."""
    with pause_rescoring():
        return run_backfill(*args, **kwargs)


def rescore_paused() -> bool:
    return not _RESCORE_PAUSE.is_set()


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
    immature: int = 0    # legacy engine only (7d defers nothing)
    live: int = 0        # 7d: provisional verdicts inside the open window
    waiting: int = 0     # 7d: market data not indexed yet, retry next pass
    birdeye_saved: int = 0  # unpriceable verdicts rescued by the Birdeye retry
    total_calls: int = 0
    message: str = ""


ProgressCb = Callable[[Progress], None]


def _noop(_p: Progress) -> None:
    pass


def _spawn_photo_fetch(channel_db_id: int, channel_ref: str) -> None:
    """Fire-and-forget: fetch a new channel's Telegram profile photo into
    frontend/public/channel_photos/<db_id>.jpg (the URL ChannelCard renders).

    Daemon thread: a slow/failed Telegram call never stalls the backfill,
    and a channel that ends up without a photo simply keeps the initials
    fallback. scripts/download_channel_photos.py covers older channels.
    """
    import os
    from config import PROJECT_ROOT

    photo = (PROJECT_ROOT / "frontend" / "public" / "channel_photos"
             / f"{channel_db_id}.jpg")
    if photo.exists():
        return

    def _worker():
        try:
            import asyncio
            from ingestion.telethon_fetcher import build_client, _resolve_entity

            async def _get():
                client = build_client()
                await client.connect()
                try:
                    if not await client.is_user_authorized():
                        return None
                    ent = await _resolve_entity(client, channel_ref)
                    photo.parent.mkdir(parents=True, exist_ok=True)
                    # extension-less stem: Telethon appends the real suffix
                    # and returns the final path -> rename to <id>.jpg.
                    stem = str(photo.parent / f"_{channel_db_id}_tmp")
                    return await client.download_profile_photo(  # type: ignore[arg-type]
                        ent, file=stem, download_big=True)
                finally:
                    await client.disconnect()  # type: ignore[misc]

            path = asyncio.run(_get())
            if path and os.path.exists(path):
                os.replace(path, photo)
                log.info("channel photo saved: %s", photo.name)
        except Exception as e:
            log.info("photo fetch skipped for %s: %s", channel_ref, e)

    threading.Thread(target=_worker, name=f"photo-{channel_db_id}",
                     daemon=True).start()


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat() + "Z"


def make_buckets(start: datetime) -> WeekBuckets:
    """Build the 12 fixed weekly buckets from a naive UTC start datetime."""
    end = start + timedelta(weeks=settings.window_weeks)
    week_starts = [start + timedelta(weeks=i) for i in range(settings.window_weeks)]
    return WeekBuckets(start=start, end=end, week_starts=week_starts)


# ── Fetch presets ──────────────────────────────────────────────────────────

PRESET_KEYS = ("1d", "3d", "7d", "1m", "2m", "3m", "4m", "5m")


def preset_window(preset: str, now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """Return (start, end) for a backfill preset. end = now; start anchored to
    the period boundary so a 1d scan always covers 24-48h, monthly presets
    start at 00:00 UTC on the 1st of the month N months back."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    if preset == "1d":
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif preset == "3d":
        start = (now - timedelta(days=3)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif preset == "7d":
        start = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif preset.endswith("m"):
        months = int(preset[:-1])
        y, m = now.year, now.month
        for _ in range(months):
            m -= 1
            if m == 0:
                m, y = 12, y - 1
        start = datetime(y, m, 1, 0, 0, 0)
    else:
        raise ValueError(f"unknown preset '{preset}'")
    return start, now


def reconcile_channel_duplicates(channel_id: int,
                                 chain: Optional[str] = None) -> int:
    """Cross-run dedup: if the same token/ticker appears as >1 call for a
    channel (because an earlier run only saw a later pump-update), keep the
    EARLIEST call and hard-delete the later ones (plus their stoploss rows).
    Returns the number of rows deleted.

    `chain` ('sol' | 'robinhood') partitions the dedup on the unified schema
    so a Solana ticker can never shadow an EVM ticker with the same symbol.
    None (legacy schema) = all rows, byte-identical to pre-cutover behavior.
    """
    conn = get_connection()
    chain_filter = "AND chain = ? " if chain is not None else ""
    params = [channel_id] + ([chain] if chain is not None else [])
    rows = conn.execute(
        f"SELECT id, token_address, token_symbol, call_timestamp FROM calls "
        f"WHERE channel_id = ? {chain_filter}ORDER BY call_timestamp ASC, id ASC",
        params,
    ).fetchall()

    seen_addr: dict = {}
    seen_sym: dict = {}
    to_delete: list = []
    for r in rows:
        addr = r["token_address"].lower()
        sym = (r["token_symbol"] or "").upper()
        key = seen_addr.get(addr) or (seen_sym.get(sym) if sym else None)
        if key is None:
            key = addr
            seen_addr[addr] = key
            if sym:
                seen_sym[sym] = key
        else:
            to_delete.append(r["id"])  # later mention of an already-seen token

    if to_delete:
        ph = ",".join("?" * len(to_delete))
        with transaction() as conn:
            conn.execute(f"DELETE FROM stoploss_results WHERE call_id IN ({ph})", to_delete)
            conn.execute(f"DELETE FROM calls WHERE id IN ({ph})", to_delete)
        log.info("reconcile: deleted %d superseded duplicate call(s) for channel %s", len(to_delete), channel_id)
    return len(to_delete)


def reconcile_by_resolved_identity(channel_id: int,
                                   chain: Optional[str] = None) -> int:
    """Post-pricing dedup: if two different addresses in the same channel resolve
    to the same dex_pool_id (same underlying token), keep the EARLIEST call and
    delete the later one. Catches 'update address' posts where the caller gives
    a new address for the same token. Returns the number of rows deleted.

    This runs AFTER pricing, when token_meta is fully populated with resolved
    pool identities — the class of bug where a caller posts address A (the real
    call), then later posts address B (an update/pump) for the same token.

    `chain` scopes the token_meta join (PK is (chain, address) on the unified
    schema) and the dedup partition; None = legacy single-chain behavior."""
    conn = get_connection()
    has_chain = _calls_has_chain_column() and chain is not None
    join_chain = "AND c.chain = tm.chain" if has_chain else ""
    where_chain = "AND c.chain = ?" if has_chain else ""
    params = [channel_id] + ([chain] if has_chain else [])
    rows = conn.execute(
        f"""
        SELECT c.id, c.token_address, c.call_timestamp, tm.dex_pool_id
        FROM calls c
        LEFT JOIN token_meta tm ON c.token_address = tm.address {join_chain}
        WHERE c.channel_id = ? AND c.status != 'pending' {where_chain}
        ORDER BY c.call_timestamp ASC, c.id ASC
        """,
        params,
    ).fetchall()

    seen_pool: dict = {}
    to_delete: list = []
    for r in rows:
        pool_id = r["dex_pool_id"]
        if not pool_id:
            continue  # unpriceable or unresolved — can't dedup by identity
        key = seen_pool.get(pool_id)
        if key is None:
            seen_pool[pool_id] = r["id"]
        else:
            to_delete.append(r["id"])  # later mention of same resolved token

    if to_delete:
        ph = ",".join("?" * len(to_delete))
        with transaction() as conn:
            conn.execute(f"DELETE FROM stoploss_results WHERE call_id IN ({ph})", to_delete)
            conn.execute(f"DELETE FROM calls WHERE id IN ({ph})", to_delete)
        log.info("reconcile-by-identity: deleted %d resolved-identity duplicate(s) for channel %s", len(to_delete), channel_id)
    return len(to_delete)


def mature_pending_calls(progress_cb: ProgressCb = _noop) -> int:
    """Silently finalize immature pending calls whose 12h window has now
    elapsed. Called on app startup. Returns the number of calls priced."""
    from pricing.backtest import backtest_call
    from pricing.geckoterminal import GeckoTerminalClient

    init_db()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    conn = get_connection()
    chain_col = ", chain" if _calls_has_chain_column() else ""
    rows = conn.execute(
        f"SELECT id, channel_id{chain_col}, token_address, call_timestamp FROM calls "
        "WHERE status = 'pending' AND pending_reason = 'immature_window' "
        "ORDER BY call_timestamp ASC"
    ).fetchall()
    if not rows:
        return 0

    client = GeckoTerminalClient()
    has_chain = _calls_has_chain_column()
    priced = 0
    for i, row in enumerate(rows, start=1):
        call_ts = datetime.fromisoformat(row["call_timestamp"].replace("Z", ""))
        if now < call_ts + timedelta(hours=maturity_hours()):
            continue  # still immature
        chain = row["chain"] if has_chain else "sol"
        try:
            result, sl_result, r7 = price_one_call(
                chain, client, row["token_address"], call_ts
            )
        except Exception as e:
            log.exception("mature_pending backtest failed for call %s", row["id"])
            from pricing.backtest import _unpriceable_pair
            result, sl_result = _unpriceable_pair(str(e))
            r7 = None
        if r7 is not None:
            apply_eval7d(row["id"], r7)
        else:
            apply_backtest(row["channel_id"], row["id"], 0, result)
        persist_stoploss_result(row["id"], sl_result)
        priced += 1
        progress_cb(Progress(stage="price", scanned=i, total_calls=len(rows), priced=priced))
    log.info("startup maturation: priced %d previously immature call(s)", priced)
    return priced


def rescore_live_calls(progress_cb: ProgressCb = _noop,
                       limit: Optional[int] = None) -> int:
    """Re-score every 'live' 7d call through the SAME engine with the
    evaluation window extended toward `now` (and pick up 'waiting_for_data'
    rows whose market data may exist by now; and 'live' win/loss rows whose
    window has since elapsed — those get their FINAL verdict here).

    Concurrency (three layers):
      * one-pass-only guard: a second concurrent call skips and flags the
        running pass to sweep AGAIN after it finishes (no duplicate fetches,
        nothing missed).
      * pipeline.pause_rescoring(): fetch/refresh streams set this while
        they run, so a background pass pauses BETWEEN CALLS until the
        backfill completes, then resumes — the user's explicit fetch always
        outranks the opportunistic refresh.
      * WAL readers (UI/API stats) are never blocked regardless.

    No per-day cap: cost is bounded by construction — closed candles come
    from price_cache, and a fresh hourly fetch happens only while a call's
    window is still open OR when it is being finalized.

    Returns the number of calls updated.
    """
    if settings.pricing_engine != "7d" or not _calls_has_chain_column():
        return 0  # legacy engine: immature rows go through mature_pending_calls
    global _RESCORE_RUNNING, _RESCORE_RERUN
    with _RESCORE_LOCK:
        if _RESCORE_RUNNING:
            _RESCORE_RERUN = True   # sweep again when the running pass ends
            log.info("rescore already running — skipped (rerun flagged)")
            return 0
        _RESCORE_RUNNING = True
    try:
        total_updated = 0
        while True:
            with _RESCORE_LOCK:
                _RESCORE_RERUN = False
            n = _rescore_pass_once(progress_cb, limit)
            total_updated += n
            with _RESCORE_LOCK:
                if not _RESCORE_RERUN:
                    break
            log.info("rescore: new work flagged during pass — sweeping again")
        return total_updated
    finally:
        with _RESCORE_LOCK:
            _RESCORE_RUNNING = False


def _rescore_pass_once(progress_cb: ProgressCb, limit: Optional[int]) -> int:
    init_db()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    conn = get_connection()
    # Thread-local connections: safe to run from a background thread.
    # Backlog migration: rows deferred as 'immature_window' under the older
    # semantics are exactly what 'live' means now — a young call whose 7d
    # window is still open. Fold them into the live set (one-time, cheap).
    conn.execute(
        "UPDATE calls SET score_state='live' "
        "WHERE status='pending' AND pending_reason='immature_window'"
    )
    rows = conn.execute(
        "SELECT id, token_address, call_timestamp, chain FROM calls "
        "WHERE score_state = 'live' AND status = 'pending' "
        "ORDER BY call_timestamp ASC"
    ).fetchall()
    # Second lane: live rows that already carry a provisional verdict.
    # NO window filter here — still-open rows get extended verdicts (new
    # candles; status may flip), and rows whose window has elapsed get
    # their FINAL verdict on this pass and leave the live set. An
    # 'evaluation_end_timestamp > now' filter would silently strand every
    # elapsed live row in 'live' forever.
    rows += conn.execute(
        "SELECT id, token_address, call_timestamp, chain FROM calls "
        "WHERE score_state = 'live' AND status IN ('win','loss') "
        "ORDER BY call_timestamp ASC"
    ).fetchall()
    if limit:
        rows = rows[:limit]
    if not rows:
        return 0

    client = GeckoTerminalClient()
    updated = 0
    for i, row in enumerate(rows, start=1):
        # Yield to user-initiated backfills: block HERE (on this same row)
        # until the fetch/refresh stream finishes, then continue normally.
        while not _RESCORE_PAUSE.wait(timeout=300):
            log.info("rescore waiting on active fetch stream (%ds)…", 300)
        call_ts = datetime.fromisoformat(row["call_timestamp"].replace("Z", ""))
        live_now = now if now < call_ts + timedelta(days=settings.eval_days) else None
        chain = row["chain"] or "sol"
        try:
            result, sl_result, r7 = price_one_call(
                chain, client, row["token_address"], call_ts, now=live_now)
        except Exception as e:
            log.exception("rescore failed for call %s", row["id"])
            if live_now is not None:
                continue  # transient error — retry next pass
            from pricing.backtest import _unpriceable_pair
            result, sl_result = _unpriceable_pair(str(e))
            r7 = None
        if r7 is None:
            continue  # engine switched to legacy mid-run; leave row alone
        state = score_state_7d(r7)
        if state == "waiting" and live_now is not None:
            mark_waiting_7d(row["id"])  # still no data; keep retrying
        elif state == "waiting":
            # window closed while we waited — finalize the spec's
            # unpriceable_loss verdict now that "no data after 7d" is a fact.
            apply_eval7d(row["id"], r7)
            persist_stoploss_result(row["id"], sl_result)
        else:
            apply_eval7d(row["id"], r7)
            persist_stoploss_result(row["id"], sl_result)
        updated += 1
        progress_cb(Progress(stage="price", scanned=i, total_calls=len(rows),
                             priced=updated, message=(
                                 f"rescoring live calls {i}/{len(rows)}"
                                 + (" (finalizing)" if live_now is None else ""))))
    log.info("live rescore: updated %d call(s)", updated)
    return updated


def _checkpoint_scanned_at(channel_id: int, window_end: datetime) -> None:
    """Record that this channel's messages are now scanned through
    `window_end` (naive UTC). Opportunistic refresh anchors here instead of
    at MAX(call.timestamp). Guarded: legacy-schema DBs have no such column,
    and a failed checkpoint must never fail a completed backfill."""
    try:
        iso = window_end.replace(microsecond=0).isoformat() + "Z"
        with transaction() as conn:
            exists = conn.execute(
                "SELECT 1 FROM pragma_table_info('channels') WHERE name='last_scanned_at'"
            ).fetchone()
            if not exists:
                return
            conn.execute(
                "UPDATE channels SET last_scanned_at=? WHERE id=?", (iso, channel_id)
            )
    except Exception:
        log.warning("failed to checkpoint last_scanned_at for channel %s", channel_id)


def find_channel_row(conn, telegram_channel_id: int,
                     username: Optional[str], title: Optional[str]):
    """Locate an existing channels row for a Telegram channel via a stable-key
    cascade.

    Telegram entity ids have twice come back TRANSIENTLY WRONG from the
    dialogs list (2026-09-17 and 2026-09-20 phantom-duplicate incidents), so
    the id only rules when it matches exactly; username (globally unique) is
    the strong fallback; title is the last resort and only matches keyless
    legacy rows (username NULL/'') which have nothing else to match on.
    Returns the row (id, username, telegram_channel_id, window_start,
    window_end) or None."""
    row = conn.execute(
        "SELECT id, username, telegram_channel_id, window_start, window_end "
        "FROM channels WHERE telegram_channel_id = ?",
        (telegram_channel_id,),
    ).fetchone()
    if row:
        return row
    if username:
        row = conn.execute(
            "SELECT id, username, telegram_channel_id, window_start, window_end "
            "FROM channels "
            "WHERE username IS NOT NULL AND username <> '' "
            "AND UPPER(username) = UPPER(?)",
            (username,),
        ).fetchone()
        if row:
            return row
    if title:
        row = conn.execute(
            "SELECT id, username, telegram_channel_id, window_start, window_end "
            "FROM channels "
            "WHERE title = ? AND (username IS NULL OR username = '')",
            (title,),
        ).fetchone()
        if row:
            return row
    return None


def ensure_channel(
    channel_ref: str,
    telegram_channel_id: int,
    title: Optional[str],
    username: Optional[str],
    window_start: datetime,
    window_end: datetime,
) -> int:
    """Insert/return the channel row for a backfill.

    Coverage is a UNION across runs: re-running a channel with a different
    preset (e.g. 1d after 5m) widens the recorded window instead of shrinking
    it, so all-time data grows indefinitely. Per-run coverage lives in
    ingestion_runs.
    """
    with transaction() as conn:
        row = find_channel_row(conn, telegram_channel_id, username, title)
        if row:
            # Stable-key hit with a DIFFERENT telegram id = id drift (see
            # 2026-09-17 / 2026-09-20 phantom incidents): adopt the new id
            # instead of minting a second row that would split the history.
            if int(row["telegram_channel_id"]) != int(telegram_channel_id):
                conn.execute(
                    "UPDATE channels SET telegram_channel_id = ? WHERE id = ?",
                    (telegram_channel_id, row["id"]),
                )
                log.warning(
                    "channel id drift adopted: %s (db id %d) telegram id %d -> %d",
                    username or title, int(row["id"]),
                    int(row["telegram_channel_id"]), telegram_channel_id,
                )
            old_start = datetime.fromisoformat(row["window_start"].replace("Z", "")) if row["window_start"] else window_start
            old_end = datetime.fromisoformat(row["window_end"].replace("Z", "")) if row["window_end"] else window_end
            new_start = min(old_start, window_start)
            new_end = max(old_end, window_end)
            conn.execute(
                "UPDATE channels SET title=?, username=?, window_start=?, window_end=? WHERE id=?",
                (title, username, _iso(new_start), _iso(new_end), row["id"]),
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


def persist_parsed_call(channel_id: int, call, week_index: int,
                        chain: str = "sol") -> None:
    """Insert a parsed call as 'pending' (idempotent on the unique key).

    `chain` is written only on the unified schema; legacy rows are all
    Solana by definition."""
    has_chain = _calls_has_chain_column()
    cols = "chain, " if has_chain else ""
    vals = "?, " if has_chain else ""
    params: list = [channel_id, call.message_id, call.raw_text[:2000],
                    call.token_address, call.token_symbol, call.token_name,
                    _iso(call.timestamp), week_index]
    if has_chain:
        params.insert(1, chain)
    with transaction() as conn:
        conn.execute(
            f"""
            INSERT OR IGNORE INTO calls
                (channel_id, {cols}message_id, raw_text, token_address, token_symbol,
                 token_name, call_timestamp, week_index, status)
            VALUES (?, {vals}?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            params,
        )


def apply_backtest(channel_id: int, call_id: int, message_id: int, result) -> None:
    """Write a priced result back to the calls row."""
    # Calculate peak_multiple from entry and peak prices
    peak_multiple = None
    if result.entry_price_usd and result.peak_price_usd and result.entry_price_usd > 0:
        peak_multiple = result.peak_price_usd / result.entry_price_usd
    
    with transaction() as conn:
        conn.execute(
            """
            UPDATE calls SET
                pool_address = ?,
                entry_price_usd = ?,
                peak_price_usd = ?,
                peak_timestamp = ?,
                peak_profit_pct = ?,
                peak_multiple = ?,
                is_win = ?,
                status = ?,
                pending_reason = NULL,
                priced_at = datetime('now')
            WHERE id = ?
            """,
            (
                result.pool_address,
                result.entry_price_usd,
                result.peak_price_usd,
                _iso(result.peak_timestamp) if result.peak_timestamp else None,
                result.peak_profit_pct,
                peak_multiple,
                1 if result.is_win else 0,
                result.status,
                call_id,
            ),
        )


def persist_stoploss_result(call_id: int, sl) -> None:
    """Write a 50% stop-loss strategy result to stoploss_results (upsert)."""
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO stoploss_results
                (call_id, entry_price_usd, peak_price_usd, peak_timestamp,
                 peak_profit_pct, hit_stoploss, stoploss_timestamp,
                 final_close_usd, final_close_ts, is_win, status, error, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(call_id) DO UPDATE SET
                entry_price_usd=excluded.entry_price_usd,
                peak_price_usd=excluded.peak_price_usd,
                peak_timestamp=excluded.peak_timestamp,
                peak_profit_pct=excluded.peak_profit_pct,
                hit_stoploss=excluded.hit_stoploss,
                stoploss_timestamp=excluded.stoploss_timestamp,
                final_close_usd=excluded.final_close_usd,
                final_close_ts=excluded.final_close_ts,
                is_win=excluded.is_win,
                status=excluded.status,
                error=excluded.error,
                computed_at=datetime('now')
            """,
            (
                call_id,
                sl.entry_price_usd,
                sl.peak_price_usd,
                _iso(sl.peak_timestamp) if sl.peak_timestamp else None,
                sl.peak_profit_pct,
                1 if sl.hit_stoploss else 0,
                _iso(sl.stoploss_timestamp) if sl.stoploss_timestamp else None,
                getattr(sl, "final_close_usd", None),
                _iso(sl.final_close_ts) if getattr(sl, "final_close_ts", None) else None,
                1 if sl.is_win else 0,
                sl.status,
                sl.error,
            ),
        )
    # Strategy 3 rides every pricing pass for free: the candles the engine
    # just fetched are in price_cache, so the trailing verdict is computed
    # cache-only (zero API). Never allowed to break the main persist path.
    try:
        score_trailing_call(call_id)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception(
            "trailing attach failed for call %s", call_id)


# ---- Strategy 3: 50% TRAILING stop (cache-only, rides every pricing pass) --

def persist_trailing_result(call_id: int, tr) -> None:
    """Write a trailing-stop result to trailing_results (upsert on call_id)."""
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO trailing_results
                (call_id, entry_price_usd, peak_multiple, peak_profit_pct,
                 peak_price_usd, peak_timestamp, exit_multiple, exit_price_usd,
                 exit_timestamp, loss_pct, hit_trailing_stop, is_win, status,
                 error, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(call_id) DO UPDATE SET
                entry_price_usd=excluded.entry_price_usd,
                peak_multiple=excluded.peak_multiple,
                peak_profit_pct=excluded.peak_profit_pct,
                peak_price_usd=excluded.peak_price_usd,
                peak_timestamp=excluded.peak_timestamp,
                exit_multiple=excluded.exit_multiple,
                exit_price_usd=excluded.exit_price_usd,
                exit_timestamp=excluded.exit_timestamp,
                loss_pct=excluded.loss_pct,
                hit_trailing_stop=excluded.hit_trailing_stop,
                is_win=excluded.is_win,
                status=excluded.status,
                error=excluded.error,
                computed_at=datetime('now')
            """,
            (
                call_id,
                tr.entry_price_usd,
                tr.peak_multiple,
                (tr.peak_multiple - 1.0) * 100.0 if tr.peak_multiple else None,
                tr.peak_price_usd,
                _iso(tr.peak_timestamp) if tr.peak_timestamp else None,
                tr.exit_multiple,
                tr.exit_price_usd,
                _iso(tr.exit_timestamp) if tr.exit_timestamp else None,
                tr.loss_pct,
                1 if tr.hit_trailing_stop else 0,
                1 if tr.is_win else 0,
                tr.status,
                tr.error,
            ),
        )


def _trailing_series(conn, call_row) -> list:
    """Candle series for one call, cache-only: dense minute bars when they
    cover the window well (>=60% of hours have >=45 min each), else hourly.
    Empty list = nothing scored yet for this pool (skip, not a verdict)."""
    ts = datetime.fromisoformat(str(call_row["call_timestamp"]).replace("Z", ""))
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    end = ts + timedelta(days=settings.eval_days)
    pool, token = call_row["pool_address"], call_row["token_address"]
    if not pool:
        return []
    hours = conn.execute(
        "SELECT candle_ts, open, high, low, close, volume FROM price_cache "
        "WHERE pool_address=? AND aggregate='hour' AND candle_ts>=? AND candle_ts<? "
        "ORDER BY candle_ts",
        (pool, _iso(ts - timedelta(hours=1)), _iso(end))).fetchall()
    mins = conn.execute(
        "SELECT candle_ts, open, high, low, close, volume FROM price_cache "
        "WHERE pool_address=? AND aggregate='minute' AND candle_ts>=? AND candle_ts<? "
        "ORDER BY candle_ts",
        (pool, _iso(ts - timedelta(hours=1)), _iso(end))).fetchall()
    from models import PricePoint
    def _pt(r):
        return PricePoint(
            timestamp=datetime.fromisoformat(str(r["candle_ts"]).replace("Z", "")),
            open=r["open"], high=r["high"], low=r["low"], close=r["close"],
            volume=r["volume"] or 0.0)
    if not hours:
        # Some pools (the fixed-stop strategy's minute-first path) cached
        # ONLY 1m bars — use them directly; finer resolution is strictly
        # better here (minute-precision exits).
        return [_pt(m) for m in mins]
    # Dense minute coverage (>=60% of the hourly span) means the engine
    # deep-checked this window; prefer minutes, else hourly.
    if mins and len(mins) >= 0.6 * len(hours) * 60:
        return [_pt(m) for m in mins]
    return [_pt(h) for h in hours]


def score_trailing_call(call_id: int) -> Optional[str]:
    """Compute + persist the trailing verdict for one call from CACHED
    candles only (zero API). Returns the status, or None when no candles
    are cached yet (call stays untrailing; the next pass retries)."""
    from pricing.trailing import score_candles_trailing
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, pool_address, token_address, call_timestamp, score_state"
            " FROM calls WHERE id=?", (call_id,)).fetchone()
        if row is None:
            return None
        candles = _trailing_series(conn, row)
    if not candles:
        return None
    ts = datetime.fromisoformat(str(row["call_timestamp"]).replace("Z", ""))
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    tr = score_candles_trailing(candles, ts,
                                win_multiplier=settings.win_multiplier
                                if hasattr(settings, "win_multiplier") else 2.0,
                                pool_address=row["pool_address"])
    if tr.status == "unpriceable_loss":
        return None
    # OPEN-WINDOW RULE: a call still inside its 7d window (score_state
    # 'live') has no expiry yet — 'expired' only becomes real at the day-7
    # cutoff. While the window is open, an un-triggered trail is 'pending'
    # (retry next pass). A TRIGGERED stop-out is final regardless of state.
    score_state = row["score_state"] if "score_state" in row.keys() else "final"
    if score_state == "live" and tr.status == "expired":
        tr.status = "pending"
    persist_trailing_result(call_id, tr)
    return tr.status


def run_trailing_backfill(progress_cb: ProgressCb = _noop,
                          limit: Optional[int] = None) -> Progress:
    """One cache-only pass: score every decided call that has no trailing
    row yet (or re-score live-window calls whose verdict may mature).
    Reads price_cache exclusively — zero network, safe to run any time."""
    init_db()
    progress = Progress(stage="price")
    progress_cb(progress)
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT cal.id FROM calls cal
               LEFT JOIN trailing_results tr ON tr.call_id = cal.id
               WHERE cal.status IN ('win','loss')
                 AND (tr.id IS NULL
                      OR cal.score_state='live'
                      OR (cal.score_state='final'
                          AND tr.status NOT IN ('win','loss','expired')))
               ORDER BY cal.id"""
            + (f" LIMIT {int(limit)}" if limit else "")
        ).fetchall()
        ids = [r["id"] for r in rows]
    total = len(ids)
    progress.total_calls = total
    progress_cb(progress)
    done = skipped = 0
    for i, cid in enumerate(ids, start=1):
        st = score_trailing_call(cid)
        if st is None:
            skipped += 1
        else:
            done += 1
        progress.priced = done
        progress.scanned = i
        progress.message = f"[{i}/{total}] trailing: {st or 'no candles yet'}"
        progress_cb(progress)
    progress.stage = "done"
    progress.message = (f"trailing pass done — {done} scored, {skipped} waiting"
                        f" on candles")
    progress_cb(progress)
    return progress


# ---- Engine dispatcher (Workstream C) --------------------------------------

_DS_CHAIN = {"sol": "solana", "robinhood": "robinhood", "eth": "ethereum", "bsc": "bsc", "base": "base", "arc": "arc"}
_v2_clients: dict = {}
_ds_client = None


def _get_v2_client(chain: str):
    """One GeckoTerminalClientV2 per network.

    Maps internal chain names to GeckoTerminal network names:
    - sol -> solana
    - robinhood -> robinhood
    - eth -> ethereum
    - bsc -> bsc
    - base -> base
    - arc -> arc

    NOTE: each client carries its own token bucket; GT's real limit is per IP
    across networks. Mixed-chain channels therefore share ~2 buckets — the
    429 circuit breaker absorbs it, and Workstream D/G price chains in
    separate passes anyway."""
    from pricing.geckoterminal_v2 import GeckoTerminalClientV2

    # Map internal chain names to GeckoTerminal NETWORK slugs.
    # NOTE: GT slugs are NOT the DexScreener ones — verified against
    # /networks: 'eth' (NOT 'ethereum'), 'bsc', 'base', 'arc', 'solana',
    # 'robinhood'. Wrong slug = 404 on every OHLCV call.
    chain_to_network = {
        "sol": "solana",
        "robinhood": "robinhood",
        "eth": "eth",
        "bsc": "bsc",
        "base": "base",
        "arc": "arc"
    }
    
    net = chain_to_network.get(chain, chain)  # Default to chain name if not in mapping
    if net not in _v2_clients:
        _v2_clients[net] = GeckoTerminalClientV2(network=net)
    return _v2_clients[net]


def _get_ds_client():
    global _ds_client
    if _ds_client is None:
        from pricing.dexscreener import DexScreenerClient
        _ds_client = DexScreenerClient()
    return _ds_client


def _calls_has_engine_column() -> bool:
    conn = get_connection()
    return any(r["name"] == "engine"
               for r in conn.execute("PRAGMA table_info(calls)").fetchall())


def _calls_has_chain_column() -> bool:
    """True on the unified schema; legacy solana_tracker.db has no chain col
    (rows there are all Solana by definition)."""
    conn = get_connection()
    return any(r["name"] == "chain"
               for r in conn.execute("PRAGMA table_info(calls)").fetchall())


def maturity_hours() -> float:
    """Horizon after which a call can be scored fairly: the 7d engine needs
    the full eval window; legacy needs its 12h peak window."""
    return (settings.eval_days * 24.0 if settings.pricing_engine == "7d"
            else float(settings.peak_window_hours))


def eval7d_to_legacy_pair(r):
    """Eval7dResult -> (BacktestResult, StoplossResult) — the mapping contract
    that keeps every legacy consumer (stoploss_results writers, analysis,
    API, UI) working unchanged under engine='7d'.

    For stoploss LOSSES the reported peak is the FULL-WINDOW peak (same
    convention legacy uses for wins); the actual stop-out moment is in
    stoploss_timestamp."""
    from models import BacktestResult, StoplossResult
    peak_pct = (r.max_multiple - 1.0) * 100.0 if r.max_multiple else None
    err = r.note if r.status_plain == "unpriceable_loss" else None
    br = BacktestResult(
        entry_price_usd=r.entry_price_usd, peak_price_usd=r.max_price_usd,
        peak_timestamp=r.time_2x_reached, peak_profit_pct=peak_pct,
        is_win=(r.status_plain == "win"), status=r.status_plain,
        pool_address=r.pool_address, error=err,
    )
    sl = StoplossResult(
        entry_price_usd=r.entry_price_usd, peak_price_usd=r.max_price_usd,
        peak_timestamp=r.time_2x_reached, peak_profit_pct=peak_pct,
        hit_stoploss=(r.status_stoploss == "loss" and r.minus_50_reached),
        stoploss_timestamp=r.time_minus_50_reached,
        is_win=(r.status_stoploss == "win"), status=r.status_stoploss,
        pool_address=r.pool_address,
        final_close_usd=r.final_close_usd,
        error=(r.note if r.status_stoploss == "unpriceable_loss" else None),
    )
    return br, sl


def apply_eval7d(call_id: int, r) -> None:
    """Write the 7d result to the calls row in ONE upsert-safe UPDATE.

    Legacy-compatible columns carry the PLAIN outcome (peak_* = full-window
    max, peak_timestamp = time_2x); every inline 7d column is stored 1:1;
    engine='7d', scored_window='7d'. Stoploss side persists through the
    existing persist_stoploss_result(eval7d_to_legacy_pair(r)[1])."""
    peak_pct = (r.max_multiple - 1.0) * 100.0 if r.max_multiple else None
    with transaction() as conn:
        conn.execute(
            """
            UPDATE calls SET
                pool_address = ?,
                entry_price_usd = ?,
                peak_price_usd = ?,
                peak_timestamp = ?,
                peak_profit_pct = ?,
                peak_multiple = ?,
                is_win = ?,
                status = ?,
                pending_reason = NULL,
                priced_at = datetime('now'),
                scored_window = '7d',
                engine = '7d',
                screening_entry_usd = ?,
                screening_target_usd = ?,
                target_usd = ?,
                max_price_usd = ?,
                min_price_usd = ?,
                max_multiple = ?,
                max_drawdown_pct = ?,
                target_2x_reached = ?,
                minus_50_reached = ?,
                time_2x_reached = ?,
                time_minus_50_reached = ?,
                which_threshold_first = ?,
                api_requests_used = ?,
                granular_analysis_required = ?,
                option2_entry = ?,
                evaluation_end_timestamp = ?,
                score_state = ?,
                note = ?,
                token_symbol = COALESCE(?, token_symbol),
                token_name = COALESCE(?, token_name),
                chain = COALESCE(?, chain)
            WHERE id = ?
            """,
            (
                r.pool_address,
                r.entry_price_usd, r.max_price_usd,
                _iso(r.time_2x_reached) if r.time_2x_reached else None,
                peak_pct, r.max_multiple,
                1 if r.status_plain == "win" else 0,
                r.status_plain,
                r.screening_entry_usd, r.screening_target_usd, r.target_usd,
                r.max_price_usd, r.min_price_usd, r.max_multiple,
                r.max_drawdown_pct,
                1 if r.target_2x_reached else 0,
                1 if r.minus_50_reached else 0,
                _iso(r.time_2x_reached) if r.time_2x_reached else None,
                _iso(r.time_minus_50_reached) if r.time_minus_50_reached else None,
                r.which_threshold_first, r.api_requests_used,
                1 if r.granular_analysis_required else 0,
                1 if r.option2_entry else 0,
                _iso(r.evaluation_end_timestamp) if r.evaluation_end_timestamp else None,
                score_state_7d(r),
                r.note,
                # DEX-resolved identity wins when present (it's canonical);
                # getattr keeps old/fake result objects without the field
                # working — None preserves the stored guess.
                getattr(r, "token_symbol", None),
                getattr(r, "token_name", None),
                # A Birdeye cross-chain rescue proved the stored chain tag
                # was wrong; adopt the true chain so every FUTURE pass
                # resolves the pool on the right network without needing
                # Birdeye again. Only the rescue sets chain_corrected —
                # normal verdicts pass None and never rewrite the tag.
                getattr(r, "chain_corrected", None),
                call_id,
            ),
        )


def score_state_7d(r) -> str:
    """Classify an Eval7dResult produced with `now` inside the 7d window.

    'final'   — window elapsed (or spec-unpriceable after day 7); never
                revisited.
    'waiting' — window open and the verdict is an unpriceable caused by
                MISSING market data (pool not indexed yet / candles not
                stored). For a young call that's not a real 'unpriceable'
                per the spec's intent — the token may list/publish hours
                later — so the row stays pending and is retried every pass.
    'live'    — window open with a PROVISIONAL verdict (win/loss on the
                observed slice). Re-scored on every pass until 'final'.
    """
    if getattr(r, "window_complete", True):
        return "final"
    if r.status_plain == "unpriceable_loss":
        note = r.note or ""
        if note in ("no pool", "no call-hour candle", "no call-minute candle",
                    "orientation ambiguous") \
                or "fetch failed" in note:
            return "waiting"
    return "live"


def _refuse_oriented(token_address: str, pool_info: Optional[dict],
                     call_ts: datetime):
    """Build the refused verdict for a CONFIRMED flipped pool (pool_guard
    cascade c): 'unpriceable_loss' with note 'orientation ambiguous' —
    score_state_7d maps it to 'waiting', so the row stays pending and every
    rescore pass retries it (GT may add a base-seat pool later; Birdeye got
    its chance at fetch time). The known-wrong curve is never scored."""
    from pricing.strategy7d import Eval7dResult
    end = call_ts + timedelta(days=settings.eval_days)
    return Eval7dResult(
        status_plain="unpriceable_loss", status_stoploss="unpriceable_loss",
        api_requests_used=0, evaluation_end_timestamp=end,
        pool_address=(pool_info or {}).get("pool_address"),
        note="orientation ambiguous", window_complete=False)


def mark_waiting_7d(call_id: int) -> None:
    """Row's 7d window is open and market data isn't there yet: keep it
    pending, tag it so run_backfill + rescore_live_calls retry it each pass."""
    # Only PENDING rows get tagged: a row that already has a provisional or
    # final verdict keeps it even if one pass transiently fails to resolve
    # (it stays 'live' and gets retried by the next rescore regardless) —
    # otherwise decided rows accumulate status=win + waiting_for_data.
    with transaction() as conn:
        conn.execute(
            "UPDATE calls SET engine='7d', score_state='live', "
            "pending_reason='waiting_for_data' "
            "WHERE id = ? AND status = 'pending'",
            (call_id,),
        )


def price_one_call(chain: str, client, token_address: str, call_ts,
                   now: Optional[datetime] = None,
                   allow_birdeye: bool = False):
    """Score one call under the configured engine.

    allow_birdeye: opt-IN cross-chain rescue on unpriceable (user policy
    2026-09-22: Birdeye may run ONLY during an initial channel fetch/scan —
    refresh, reprice and live rescore must never call it).

    `now` (7d engine only): LIVE mode truncates the evaluation window at
    `now`, producing a PROVISIONAL verdict for calls younger than
    eval_days — see pricing/strategy7d.py. None = full frozen-spec window.

    -> (BacktestResult, StoplossResult, Eval7dResult|None). A None third
    element means the legacy engine produced the pair; non-7d callers can
    ignore it. engine='7d' resolves the pool (token_meta cache -> DexScreener
    -> Solana-only GeckoTerminal smart fallback), runs the pure strategy7d
    engine through aggregate-aware cached fetchers, and maps the outcome back
    into the legacy pair shape via eval7d_to_legacy_pair."""
    if settings.pricing_engine == "legacy":
        if chain != "sol":
            raise RuntimeError(
                "legacy pricing engine has no robinhood path — set PRICING_ENGINE=7d"
            )
        result, sl = backtest_call(client, token_address, call_ts)
        return result, sl, None
    if not _calls_has_engine_column():
        raise RuntimeError(
            "7d engine requires the unified schema — set SCHEMA_FILE=schema_unified.sql"
        )
    from pricing.strategy7d import evaluate_call_7d
    from pricing.strategy7d_cache import make_cached_fetchers
    from pricing.backtest import get_cached_pool, upsert_token_meta

    pool_info = get_cached_pool(token_address, chain)
    if pool_info is None:
        resolved = _get_ds_client().resolve_pool(token_address, chain=_DS_CHAIN[chain])
        if (resolved is None or not resolved.get("pool_address")) and chain == "sol":
            # GT smart resolution is the Solana-only legacy fallback — the
            # client here is pinned to the solana network, and EVM chains get
            # DS search fallback (resolve_by_pool) inside resolve_pool itself.
            try:
                resolved = client.resolve_pool_smart(token_address)
            except Exception:  # noqa: BLE001
                resolved = None
        if resolved and resolved.get("pool_address"):
            pool_info = {
                "token_address": resolved.get("token_address") or token_address,
                "pool_address": resolved["pool_address"],
                "symbol": resolved.get("symbol"),
                "name": resolved.get("name"),
                "liquidity_usd": resolved.get("liquidity_usd"),
            }
            upsert_token_meta(pool_info, chain=chain)
            # When the QUERY was itself a pool key (dexscreener/geckoterminal
            # links carry the 0x+64 composite pair id, not the mint), keep the
            # mapping resolvable under the queried address too: next passes hit
            # the token_meta cache instead of re-searching (and post-pricing
            # identity dedup can then match it against mint-keyed rows).
            if (resolved.get("token_address")
                    and resolved["token_address"].lower() != token_address.lower()):
                upsert_token_meta({**pool_info, "token_address": token_address},
                                  chain=chain)
        else:
            pool_info = None

    fetch_hourly, fetch_minute = make_cached_fetchers(
        get_connection(), _get_v2_client(chain)
    )

    # ---- pool-orientation guard (museic/DRUGS lesson, 2026-09-23) ----------
    # GT's OHLCV tracks the pool's BASE seat; if the called token sits in the
    # QUOTE seat we'd score the *other* asset's curve (museic was priced on
    # META's $750 stock curve and printed a false loss). Two FREE tripwires
    # over data already in hand (exotic quote symbol / curve-vs-quote
    # magnitude); only when one fires do we spend ONE GT listing call, whose
    # verdict caches forever in token_meta.pool_is_base. Cascade on a
    # confirmed flip: (a) healthy base-seat GT pool -> same engine on it;
    # (b) none -> Birdeye (address-keyed, seat-immune; fetch-time only);
    # (c) neither -> refuse (pending, note 'orientation ambiguous') — never
    # score a known-wrong curve. POOL_GUARD=false opts out. pool_guard.py.
    guard_active = (pool_info is not None and settings.pool_guard
                    and settings.pricing_engine == "7d")
    guard = None                                   # None | 'flipped'
    if guard_active and pool_info:
        from pricing import pool_guard
        ext = _DS_CHAIN.get(chain, chain)
        seat = pool_info.get("pool_is_base")       # None | 0 | 1
        if seat == 0:
            guard = "flipped"                      # cached verdict: don't score
        elif seat is None and pool_guard.is_exotic_quote(
                ext, pool_info.get("quote_symbol")):
            is_base, cands = pool_guard.confirm_seats(
                _get_v2_client(chain), token_address, pool_info["pool_address"])
            # is_base None = listing failed -> fail open (old behavior,
            # magnitude tripwire still applies post-score)
            if is_base is not None:
                if is_base:
                    pool_info = {**pool_info, "pool_is_base": 1}
                    upsert_token_meta(pool_info, chain=chain)
                elif cands:
                    base = cands[0]                # (a) re-seat, highest reserve
                    pool_info = {**pool_info,
                                 "pool_address": base["pool_address"],
                                 "liquidity_usd": base["reserve_usd"],
                                 "pool_is_base": 1}
                    upsert_token_meta(pool_info, chain=chain)
                    log.info("pool_guard: %s re-seated to base pool %s (%s, $%s reserve)",
                             token_address[:10], base["pool_address"][:14],
                             base["name"], int(base["reserve_usd"]))
                else:
                    pool_info = {**pool_info, "pool_is_base": 0}
                    upsert_token_meta(pool_info, chain=chain)
                    guard = "flipped"              # (b)/(c) below

    if guard == "flipped":
        r = _refuse_oriented(token_address, pool_info, call_ts)
    else:
        r = evaluate_call_7d(
            pool_info["pool_address"] if pool_info else None,
            token_address, call_ts, fetch_hourly, fetch_minute,
            eval_days=settings.eval_days,
            entry_grace_minutes=settings.entry_grace_minutes,
            now=now,
        )
    r.pool_price_usd = float((pool_info or {}).get("price_usd") or 0) or None
    if pool_info:
        # Carry the resolved identity onto the verdict so apply_eval7d can
        # fill calls.token_symbol/token_name — every priced call shows its
        # real ticker (user report: deep-dive was all bare addresses).
        r.token_symbol = r.token_symbol or pool_info.get("symbol")
        r.token_name = r.token_name or pool_info.get("name")

    # magnitude tripwire post-score, for pools the seat listing never
    # confirmed (call failed, or quote looked safe): a YOUNG/LIVE window's
    # curve must sit near the token's own fresh DexScreener quote — both
    # describe "now", so a >20x divergence IS the flip evidence, no listing
    # needed. Months-old backfill windows are exempt (legit drift); they can
    # only ever be caught by the exotic-quote tripwire anyway.
    if (guard_active and pool_info and guard is None and not r.window_complete
            and pool_info.get("pool_is_base") is None
            and r.status_plain != "unpriceable_loss"):
        from pricing import pool_guard
        if pool_guard.magnitude_suspect(
                r.final_close_usd or r.max_price_usd, r.pool_price_usd, None):
            pool_info = {**pool_info, "pool_is_base": 0}
            upsert_token_meta(pool_info, chain=chain)
            r = _refuse_oriented(token_address, pool_info, call_ts)

    if r.note == "orientation ambiguous":
        # (b) last resort before refusing: Birdeye, address-keyed — allowed
        # only when the caller says so (initial fetch), per standing policy.
        if allow_birdeye:
            from pricing.birdeye_rescue import try_rescue
            rr = try_rescue(token_address, chain, call_ts, now=now)
            if rr is not None:
                r = rr
            # else: (c) stays refused — open window retried each pass
    if r.status_plain == "unpriceable_loss" and allow_birdeye \
            and r.note != "orientation ambiguous":
        # Second opinion before condemning: the 2026-09 DB-wide sweep proved
        # 195/255 'unpriceable' rows had complete history on Birdeye (wrong
        # chain tag / aggregator blind spot). Cross-chain probe is address-
        # first by design; returns None (verdict stands) on ANY failure,
        # no key, or BIRDEYE_RESCUE=false. See pricing/birdeye_rescue.py.
        from pricing.birdeye_rescue import try_rescue
        rr = try_rescue(token_address, chain, call_ts, now=now)
        if rr is not None:
            r = rr
    result, sl = eval7d_to_legacy_pair(r)
    return result, sl, r


def run_backfill(
    channel_ref: str,
    window_start: datetime,
    window_end: datetime,
    title: Optional[str] = None,
    username: Optional[str] = None,
    fetch_limit: Optional[int] = None,
    pricing_source: str = "geckoterminal",
    progress_cb: ProgressCb = _noop,
    opportunistic: bool = False,
    birdeye_rescue: bool = False,
) -> Progress:
    """
    Execute the full backfill for one channel/window. Resumable.

    channel_ref: the value passed to Telethon (e.g. '@some_channel').
    window_start/window_end: naive UTC datetimes.
    birdeye_rescue: cross-chain Birdeye second opinion on unpriceables —
        user policy (2026-09-22): ONLY an initial channel fetch/scan may
        use it (fetch stream + Fetch button + backfill scripts pass True).
        The refresh stream never passes it; live rescore / reprice call
        price_one_call directly and can never touch Birdeye.
    opportunistic: background refresh runs pass True — their Telegram fetch
        yields to a user-initiated fetch (see ingestion.telegram_guard). On
        yield the run returns early with stage='yielded' WITHOUT persisting
        anything, so the next refresh redoes this window from the same start.
    """
    init_db()
    progress = Progress(stage="fetch")
    progress_cb(progress)

    buckets = make_buckets(window_start)

    # 1. FETCH ---------------------------------------------------------------
    fetched = []
    progress.stage = "fetch"
    progress.message = (
        f"fetching messages starting from {window_start:%d %b %Y}"
    )
    progress_cb(progress)
    try:
        def on_scan(n):
            progress.scanned = n
            progress.message = f"{n:,} messages scanned…"
            progress_cb(progress)

        fetch_window_sync = _import_fetch_window_sync()
        fetched = fetch_window_sync(
            channel_ref,
            window_start,
            window_end,
            limit=fetch_limit,
            progress_cb=on_scan,
            opportunistic=opportunistic,
        )
    except Exception as e:
        from ingestion.telegram_guard import TelegramYield
        if isinstance(e, TelegramYield):
            # A user fetch took (or wants) the Telegram session. Nothing was
            # persisted for this channel — bail quietly; the frontend resumes
            # the refresh after the fetch, which redoes this window.
            log.info("refresh yielded to user fetch — %s", channel_ref)
            progress.stage = "yielded"
            progress.message = "yielded to user fetch — will retry after"
            progress_cb(progress)
            return progress
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
    # Freeze the message-scan total NOW: progress.scanned gets reused by the
    # pricing loop as a call index, and ingestion_runs must not record that.
    messages_scanned = progress.scanned
    # Resolve channel id from fetched messages (all share the same source id).
    tg_id = fetched[0].channel_id if fetched else abs(hash(channel_ref)) % (10 ** 12)
    channel_id = ensure_channel(channel_ref, tg_id, title or fetched_title, username, window_start, window_end)

    # New channels need a card photo (frontend serves /channel_photos/<id>.jpg).
    # Fire-and-forget: never blocks or fails the backfill, silent if Telegram
    # auth is busy. scripts/download_channel_photos.py covers history.
    try:
        _spawn_photo_fetch(channel_id, channel_ref)
    except Exception:
        log.debug("photo fetch kickoff skipped for channel %s", channel_id)

    # 2. PARSE (all chains) → deduplicate → persist ------------------------
    # One fetch, multiple parsers over the SAME message list:
    # - Solana base58 mints via ingestion.address_parser
    # - EVM addresses (0x) via chains.robinhood_impl.parser, then chain detection
    # The alphabets are disjoint (base58 excludes '0'), so the two passes can never collide.
    progress.stage = "parse"
    dual_chain = _calls_has_chain_column()
    sol_parsed: list = []
    evm_parsed: list = []  # All EVM addresses, chain detection happens next
    for msg in fetched:
        parsed = parse_message(msg)
        if parsed is not None:
            sol_parsed.append(parsed)
        if dual_chain:
            from models import ParsedCall as _RHCall  # same field names
            from chains.robinhood_impl import parser as rh_parser
            for rh_call in rh_parser.parse_calls(
                msg.channel_id, msg.message_id, msg.text, msg.timestamp
            ):
                evm_parsed.append({
                    "call": _RHCall(
                        channel_id=rh_call.channel_id,
                        message_id=rh_call.message_id,
                        raw_text=rh_call.raw_text,
                        token_address=rh_call.token_address,
                        token_symbol=rh_call.token_symbol,
                        token_name=None,
                        timestamp=rh_call.timestamp,
                    ),
                    "text": msg.text,  # Keep text for chain detection
                })
    raw_count = len(sol_parsed) + len(evm_parsed)
    # Sort chronologically (oldest first) BEFORE dedup so the FIRST call
    # (not the latest update/pump post) is the one we keep. Dedup is
    # per-chain: a token called on both chains is legitimately two rows.
    sol_parsed.sort(key=lambda c: c.timestamp)
    sol_parsed = deduplicate_calls(sol_parsed)
    evm_parsed.sort(key=lambda c: c["call"].timestamp)
    # Dedup EVM addresses (same address on same chain = duplicate)
    seen_evm = set()
    deduped_evm = []
    for item in evm_parsed:
        addr = item["call"].token_address.lower()
        if addr not in seen_evm:
            seen_evm.add(addr)
            deduped_evm.append(item)
    evm_parsed = deduped_evm
    deduped_count = len(sol_parsed) + len(evm_parsed)
    
    # Detect chains for all EVM addresses
    from chains.chain_detector import detect_chain
    chain_groups = {"sol": sol_parsed}  # Solana calls are already grouped
    for item in evm_parsed:
        detected_chain = detect_chain(item["text"], item["call"].token_address)
        if detected_chain not in chain_groups:
            chain_groups[detected_chain] = []
        chain_groups[detected_chain].append(item["call"])
    
    # Persist all chains
    for chain_name, calls in chain_groups.items():
        for parsed in calls:
            week_index = buckets.week_index(parsed.timestamp)
            if week_index == 0:
                week_index = max(1, min(settings.window_weeks, week_index or 1))
            persist_parsed_call(channel_id, parsed, week_index, chain=chain_name)
    
    progress.found = deduped_count
    progress.total_calls = deduped_count
    progress.message = (
        f"running regex — {len(fetched):,} messages scanned, "
        f"{deduped_count} token addresses found"
        + (f" ({raw_count - deduped_count} pump-updates removed)"
           if raw_count != deduped_count else "")
    )
    progress_cb(progress)
    if raw_count != deduped_count:
        log.info(
            "dedup: %d raw calls -> %d after window-wide dedup (%d pump-updates removed)",
            raw_count, deduped_count, raw_count - deduped_count,
        )
    
    # Log chain distribution
    chain_summary = ", ".join(f"{len(calls)} {chain}" for chain, calls in chain_groups.items() if calls)
    if chain_summary:
        log.info("multi-chain: %s", chain_summary)

    # 2b. Cross-run reconciliation: a previous (shorter) run may have inserted
    #     a later pump-update as "the call"; now that a longer window sees the
    #     true first call, hard-delete the superseded duplicates (oldest wins).
    #     Partitioned per chain on the unified schema (a sol ticker must never
    #     shadow an EVM ticker with the same symbol).
    if dual_chain:
        for chain_name in chain_groups.keys():
            if chain_groups[chain_name]:  # Only reconcile chains that have calls
                reconcile_channel_duplicates(channel_id, chain=chain_name)
    else:
        reconcile_channel_duplicates(channel_id)

    # 3. PRICE all pending calls for this channel ---------------------------
    progress.stage = "price"
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    conn = get_connection()
    pending = conn.execute(
        "SELECT id, message_id, chain, token_address, token_symbol, raw_text, "
        "call_timestamp FROM calls "
        "WHERE channel_id = ? AND status = 'pending' ORDER BY call_timestamp ASC",
        (channel_id,),
    ).fetchall()
    progress.total_calls = len(pending)
    progress.message = (
        f"{len(pending)} token calls found — checking pools via "
        f"DexScreener + GeckoTerminal…"
        if pending else "no new calls to price"
    )
    log.info(
        "pricing %d pending calls for channel %s "
        "(tokens discovered: %s)",
        len(pending), channel_ref,
        ", ".join(row["token_address"][:8] + "…" for row in pending[:5])
        + (f" (+{len(pending)-5} more)" if len(pending) > 5 else ""),
    )
    progress_cb(progress)

    client = GeckoTerminalClient()
    has_chain = _calls_has_chain_column()
    from pricing.birdeye import BirdeyeClient
    birdeye_client = BirdeyeClient()
    priced = 0
    unpriceable = 0
    # Track per-chain pricing stats
    chain_priced = {}  # chain -> count of priced calls
    chain_unpriceable = {}  # chain -> count of unpriceable calls
    live = 0
    waiting = 0
    birdeye_saved = 0  # unpriceable verdicts rescued by the Birdeye retry
    for i, row in enumerate(pending, start=1):
        addr = row["token_address"]
        sym = ""  # we don't have it in the pending row; use the address prefix
        log.info(
            "[%d/%d] requesting data for %s…",
            i, len(pending), addr[:12] + "…" + addr[-4:],
        )
        call_ts = datetime.fromisoformat(row["call_timestamp"].replace("Z", ""))
        end7 = call_ts + timedelta(days=settings.eval_days)
        engine7d = settings.pricing_engine == "7d"
        if not engine7d and now_utc < call_ts + timedelta(hours=maturity_hours()):
            # Legacy engine (12h horizon) keeps the old defer path.
            with transaction() as conn:
                conn.execute(
                    "UPDATE calls SET status='pending', pending_reason='immature_window' WHERE id=?",
                    (row["id"],),
                )
            progress.immature += 1
            progress.scanned = i
            progress.message = (
                f"[{i}/{len(pending)}] {int(maturity_hours())}h window not "
                f"elapsed yet — deferred"
            )
            progress_cb(progress)
            continue
        # 7d engine: score EVERY call, young ones included. `now` inside the
        # window truncates it -> PROVISIONAL verdict (score_state='live'),
        # refreshed toward final by rescore_live_calls on every run/launch.
        live_now = now_utc if (engine7d and now_utc < end7) else None
        chain = row["chain"] if has_chain else "sol"
        r7 = None
        try:
            if pricing_source == "birdeye" and settings.pricing_engine == "legacy":
                from pricing.backtest import backtest_call_birdeye
                result, sl_result = backtest_call_birdeye(birdeye_client, addr, call_ts)
            else:
                result, sl_result, r7 = price_one_call(chain, client, addr, call_ts,
                                                       now=live_now,
                                                       allow_birdeye=birdeye_rescue)
                # Birdeye fallback: if GeckoTerminal couldn't price this token
                # (no pool / no candles), try Birdeye which doesn't need pool
                # resolution — it takes the token address directly.
                # 7d-engine rows skip this: evaluate_call_7d is the authority.
                if r7 is None and result.status == "unpriceable_loss":
                    from pricing.backtest import backtest_call_birdeye
                    
                    # Check if addr is a pool address or token address
                    # Birdeye needs token mints, not pool addresses
                    birdeye_addr = addr
                    meta_row = conn.execute(
                        "SELECT address FROM token_meta WHERE dex_pool_id = ?",
                        (addr,)
                    ).fetchone()
                    if meta_row:
                        # addr is a pool address, get the token mint
                        birdeye_addr = meta_row[0]
                        log.info(
                            "birdeye fallback: resolved pool %s → token %s",
                            addr[:12] + "…", birdeye_addr[:12] + "…"
                        )
                    
                    log.info(
                        "gecko unpriceable for %s — falling back to birdeye",
                        addr[:12] + "…",
                    )
                    result, sl_result = backtest_call_birdeye(
                        birdeye_client, birdeye_addr, call_ts,
                    )
        except Exception as e:
            log.exception("backtest failed for call %s", row["id"])
            # Live (window open) call with a transient error -> retry next
            # pass, never lock in a loss on a glitch. Frozen/legacy window:
            # treat unexpected errors as unpriceable losses (don't block run).
            if live_now is not None:
                mark_waiting_7d(row["id"])
                waiting += 1
                progress.waiting = waiting
                progress.scanned = i
                progress.message = f"[{i}/{len(pending)}] data fetch error — will retry"
                progress_cb(progress)
                continue
            from pricing.backtest import _unpriceable_pair
            result, sl_result = _unpriceable_pair(str(e))
            r7 = None
        if r7 is not None and score_state_7d(r7) == "waiting":
            # Young call, market data not indexed yet (no pool / no candles):
            # stay pending with a retry tag instead of finalizing unpriceable.
            mark_waiting_7d(row["id"])
            waiting += 1
            progress.waiting = waiting
            progress.scanned = i
            progress.message = (
                f"[{i}/{len(pending)}] market data not indexed yet — "
                f"will retry on next refresh"
            )
            progress_cb(progress)
            continue
        if r7 is not None:
            # Birdeye-retry visibility (user 2026-09-23): a rescued verdict
            # carries chain_corrected and/or a '(birdeye <chain>)' note set
            # by pricing/birdeye_rescue.py — count it so the scan narration
            # and final report show how many GT-blind-spot tokens only the
            # Birdeye retry could price.
            if getattr(r7, "chain_corrected", None) or "birdeye" in (r7.note or ""):
                birdeye_saved += 1
            apply_eval7d(row["id"], r7)
        else:
            apply_backtest(channel_id, row["id"], row["message_id"], result)
        persist_stoploss_result(row["id"], sl_result)
        if r7 is not None and not r7.window_complete:
            live += 1  # provisional verdict; will be rescored toward final

        if result.status == "unpriceable_loss":
            unpriceable += 1
            chain_unpriceable[chain] = chain_unpriceable.get(chain, 0) + 1
            # Log unpriceable tokens to the terminal (time + message excerpt)
            # so you can investigate them.
            log.warning(
                "[UNPRICEABLE] %s | %s | %s | msg: %.120s",
                row["call_timestamp"],
                row.get("token_symbol") or "?",
                addr,
                (row.get("raw_text") or "").replace("\n", " ").strip(),
            )
        else:
            priced += 1
            chain_priced[chain] = chain_priced.get(chain, 0) + 1
        progress.priced = priced
        progress.unpriceable = unpriceable
        progress.live = live
        progress.waiting = waiting
        progress.birdeye_saved = birdeye_saved
        progress.scanned = i
        tail = []
        if live:
            tail.append(f"{live} live")
        if waiting:
            tail.append(f"{waiting} waiting for data")
        if unpriceable:
            tail.append(f"{unpriceable} not found")
        if birdeye_saved:
            tail.append(f"{birdeye_saved} via birdeye")
        if progress.immature:
            tail.append(f"{progress.immature} deferred")
        progress.message = (
            f"pricing call {i}/{len(pending)} — {priced} scored"
            + (f" ({'; '.join(tail)})" if tail else "")
        )
        progress_cb(progress)

    # 4. Post-pricing reconciliation by resolved identity: if two different
    #    addresses in this channel resolve to the same dex_pool_id (same token),
    #    the later one is an "update address" post — delete it (oldest wins).
    if dual_chain:
        reconcile_by_resolved_identity(channel_id, chain="sol")
        if any(r["chain"] == "robinhood" for r in pending):
            reconcile_by_resolved_identity(channel_id, chain="robinhood")
    else:
        reconcile_by_resolved_identity(channel_id)

    # 5. Record run(s) --------------------------------------------------------
    # Unified schema: one ingestion_runs row per chain that produced calls
    # Log ingestion runs per chain. Legacy schema has no chain column -> single combined row.
    conn = get_connection()
    run_has_chain = any(r["name"] == "chain"
                        for r in conn.execute("PRAGMA table_info(ingestion_runs)").fetchall())
    started_iso = _iso(datetime.now(timezone.utc).replace(tzinfo=None))
    with transaction() as conn:
        if run_has_chain:
            # Build per-chain stats from chain_groups
            runs = []
            for chain_name, calls in chain_groups.items():
                if calls:  # Only log chains that have calls
                    chain_found = len(calls)
                    chain_priced_count = chain_priced.get(chain_name, 0)
                    chain_unpriceable_count = chain_unpriceable.get(chain_name, 0)
                    runs.append((chain_name, chain_found, chain_priced_count, chain_unpriceable_count))
            for chain_name, found_n, priced_n, unp_n in runs:
                conn.execute(
                    """
                    INSERT INTO ingestion_runs
                        (channel_id, chain, started_at, finished_at, mode, messages_scanned,
                         calls_found, calls_priced, calls_unpriceable, status)
                    VALUES (?, ?, ?, datetime('now'), 'resume', ?, ?, ?, ?, 'completed')
                    """,
                    (channel_id, chain_name, started_iso, messages_scanned,
                     found_n, priced_n, unp_n),
                )
        else:
            conn.execute(
                """
                INSERT INTO ingestion_runs
                    (channel_id, started_at, finished_at, mode, messages_scanned,
                     calls_found, calls_priced, calls_unpriceable, status)
                VALUES (?, ?, datetime('now'), 'resume', ?, ?, ?, ?, 'completed')
                """,
                (
                    channel_id,
                    started_iso,
                    messages_scanned,
                    progress.found,
                    priced,
                    unpriceable,
                ),
            )

    # Scan checkpoint: the FETCH walked every message up to this run's
    # window_end, so opportunistic refreshes can anchor here instead of at
    # MAX(call.timestamp) — which for a quiet channel means re-scanning its
    # whole silent backlog on every boot. Only reached on the completed path
    # (error/yield returned earlier), so the checkpoint never over-promises.
    _checkpoint_scanned_at(channel_id, window_end)

    progress.stage = "done"
    tail = []
    if unpriceable:
        tail.append(f"{unpriceable} not found")
    if birdeye_saved:
        tail.append(f"{birdeye_saved} rescued via birdeye")
    if live:
        tail.append(f"{live} live (refresh to update)")
    if waiting:
        tail.append(f"{waiting} waiting for data")
    if progress.immature:
        tail.append(f"{progress.immature} deferred")
    progress.message = (
        f"done — {priced} scored" + (f" ({'; '.join(tail)})" if tail else "")
        if pending else "done — no new calls"
    )
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
    chain_col = ", chain" if _calls_has_chain_column() else ""
    rows = conn.execute(
        f"SELECT id, message_id{chain_col}, token_address, token_symbol, raw_text, call_timestamp "
        f"FROM calls WHERE channel_id = ? AND status IN ({placeholders}) "
        f"ORDER BY call_timestamp ASC",
        [channel_id, *statuses],
    ).fetchall()
    has_chain = _calls_has_chain_column()

    progress.total_calls = len(rows)
    progress_cb(progress)
    if not rows:
        progress.stage = "done"
        progress.message = "no calls to re-price"
        progress_cb(progress)
        return progress

    log.info("re-pricing %d calls for channel %d (statuses: %s)", len(rows), channel_id, statuses)

    client = GeckoTerminalClient()
    now_utc_reprice = datetime.now(timezone.utc).replace(tzinfo=None)
    priced = 0
    unpriceable = 0

    for i, row in enumerate(rows, start=1):
        addr = row["token_address"]
        chain = row["chain"] if has_chain else "sol"
        call_ts = datetime.fromisoformat(row["call_timestamp"].replace("Z", ""))
        # Legacy engine defers immature calls; the 7d engine scores them LIVE
        # (provisional verdict, refreshed by rescore_live_calls).
        engine7d = settings.pricing_engine == "7d"
        if not engine7d and now_utc_reprice < call_ts + timedelta(hours=maturity_hours()):
            with transaction() as conn:
                conn.execute(
                    "UPDATE calls SET status='pending', pending_reason='immature_window' WHERE id=?",
                    (row["id"],),
                )
            continue
        live_now = (now_utc_reprice
                    if engine7d and now_utc_reprice < call_ts + timedelta(days=settings.eval_days)
                    else None)
        try:
            result, sl_result, r7 = price_one_call(chain, client, addr, call_ts,
                                                   now=live_now)
        except Exception as e:
            log.exception("reprice backtest failed for call %s", row["id"])
            if live_now is not None:
                mark_waiting_7d(row["id"])
                continue  # transient error on a live call — retry later
            from pricing.backtest import _unpriceable_pair
            result, sl_result = _unpriceable_pair(str(e))
            r7 = None
        if r7 is not None and score_state_7d(r7) == "waiting" and live_now is not None:
            mark_waiting_7d(row["id"])
            continue
        if r7 is not None:
            apply_eval7d(row["id"], r7)
        else:
            apply_backtest(channel_id, row["id"], row["message_id"], result)
        persist_stoploss_result(row["id"], sl_result)

        if result.status == "unpriceable_loss":
            unpriceable += 1
            log.warning(
                "[UNPRICEABLE] %s | %s | %s | msg: %.120s",
                row["call_timestamp"],
                row.get("token_symbol") or "?",
                addr,
                (row.get("raw_text") or "").replace("\n", " ").strip(),
            )
        else:
            priced += 1
        progress.priced = priced
        progress.unpriceable = unpriceable
        progress.scanned = i
        progress_cb(progress)

    # Post-pricing reconciliation by resolved identity (same as backfill):
    # if two different addresses resolve to the same dex_pool_id, delete the later.
    reconcile_by_resolved_identity(channel_id)

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
    summary = {"calls_deleted": 0, "runs_deleted": 0, "stoploss_deleted": 0, "channel_deleted": False}

    with transaction() as conn:
        # Delete stoploss_results first (references calls)
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM stoploss_results WHERE call_id IN (SELECT id FROM calls WHERE channel_id = ?)",
            (channel_id,)
        ).fetchone()
        summary["stoploss_deleted"] = count["n"] if count else 0
        conn.execute(
            "DELETE FROM stoploss_results WHERE call_id IN (SELECT id FROM calls WHERE channel_id = ?)",
            (channel_id,)
        )

        # Then delete calls (references channels)
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM calls WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        summary["calls_deleted"] = count["n"] if count else 0
        conn.execute("DELETE FROM calls WHERE channel_id = ?", (channel_id,))

        # Then delete ingestion_runs (references channels)
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM ingestion_runs WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        summary["runs_deleted"] = count["n"] if count else 0
        conn.execute("DELETE FROM ingestion_runs WHERE channel_id = ?", (channel_id,))

        # Finally delete the channel itself
        if delete_channel:
            conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
            summary["channel_deleted"] = True

    log.info(
        "deleted channel %d data: %d calls, %d runs, %d stoploss, channel=%s",
        channel_id, summary["calls_deleted"], summary["runs_deleted"], summary["stoploss_deleted"], summary["channel_deleted"],
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
    from pricing.backtest import score_candles, score_candles_stoploss
    from models import BacktestResult

    init_db()
    progress = Progress(stage="price")
    progress_cb(progress)

    conn = get_connection()

    if call_ids:
        placeholders = ",".join("?" * len(call_ids))
        rows = conn.execute(
            f"SELECT id, message_id, token_address, token_symbol, raw_text, call_timestamp "
            f"FROM calls WHERE id IN ({placeholders}) "
            f"ORDER BY call_timestamp ASC",
            call_ids,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, message_id, token_address, token_symbol, raw_text, call_timestamp FROM calls "
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
        peak = settings.peak_window_hours
        start_ts = call_ts - timedelta(minutes=5)
        end_ts = start_ts + timedelta(hours=peak)

        try:
            # Single 1-minute fetch over the peak window (12h) covers BOTH the
            # call-minute entry price and the peak scan.
            candles = client.fetch_ohlcv(
                addr,
                start_ts,
                end_ts,
                interval="1m",
            )
        except BirdeyeError as e:
            log.warning("Birdeye pricing failed for %s: %s", addr[:12] + "...", e)
            result = BacktestResult(
                entry_price_usd=None, peak_price_usd=None, peak_timestamp=None,
                peak_profit_pct=None, is_win=False, status="unpriceable_loss",
                error=f"Birdeye: {e}",
            )
            from models import StoplossResult
            sl_result = StoplossResult(
                entry_price_usd=None, peak_price_usd=None, peak_timestamp=None,
                peak_profit_pct=None, hit_stoploss=False, stoploss_timestamp=None,
                is_win=False, status="unpriceable_loss", error=f"Birdeye: {e}",
            )
            persist_stoploss_result(row["id"], sl_result)
            unpriceable += 1
            log.warning(
                "[UNPRICEABLE-BIRDEYE] %s | %s | %s | msg: %.120s",
                row["call_timestamp"], row.get("token_symbol") or "?", addr,
                (row.get("raw_text") or "").replace("\n", " ").strip(),
            )
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

        # Score using the same logic as GeckoTerminal backtest (both strategies).
        result = score_candles(candles, call_ts, addr)
        sl_result = score_candles_stoploss(candles, call_ts, addr)

        conn.execute(
            """UPDATE calls SET
                pool_address = ?,
                entry_price_usd = ?,
                peak_price_usd = ?,
                peak_timestamp = ?,
                peak_profit_pct = ?,
                is_win = ?,
                status = ?,
                pending_reason = NULL,
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
        persist_stoploss_result(row["id"], sl_result)

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


# ── 50% Stop-Loss Strategy ──────────────────────────────────────────────────

def run_stoploss_backtest(
    channel_id: int | None = None,
    limit: int | None = None,
    progress_cb: ProgressCb = _noop,
) -> Progress:
    """
    Run the 50% stop-loss strategy on all priced calls.

    For each call, fetches hourly candles from Birdeye for the 7-day window,
    then runs score_candles_stoploss(). If both the 2x target and 50% stop-loss
    are hit in the same hour, fetches 1m candles for that specific hour to
    resolve the exact chronological order.

    Stores results in the stoploss_results table.
    """
    from pricing.cache import load_candles
    from pricing.backtest import score_candles_stoploss, StoplossResult

    init_db()
    init_stoploss_table()
    progress = Progress(stage="price")
    progress_cb(progress)

    conn = get_connection()

    if channel_id:
        query = (
            "SELECT id, token_address, token_symbol, call_timestamp, "
            "entry_price_usd "
            "FROM calls WHERE channel_id = ? AND status IN ('win','loss') "
            "AND entry_price_usd IS NOT NULL "
            "ORDER BY call_timestamp ASC"
        )
        params: list = [channel_id]
    else:
        query = (
            "SELECT id, token_address, token_symbol, call_timestamp, "
            "entry_price_usd "
            "FROM calls WHERE status IN ('win','loss') "
            "AND entry_price_usd IS NOT NULL "
            "ORDER BY call_timestamp ASC"
        )
        params = []

    if limit:
        query += " LIMIT ?"
        params.append(int(limit))

    rows = conn.execute(query, params).fetchall()

    progress.total_calls = len(rows)
    progress_cb(progress)
    if not rows:
        progress.stage = "done"
        progress.message = "no priced calls to backtest"
        progress_cb(progress)
        return progress

    log.info("stoploss backtest: %d calls to process", len(rows))
    done = 0
    errors = 0

    for i, row in enumerate(rows, start=1):
        call_id = row["id"]
        token_address = row["token_address"]
        call_ts = datetime.fromisoformat(row["call_timestamp"].replace("Z", ""))
        window = settings.peak_window_hours
        end_ts = call_ts + timedelta(hours=window)

        # Check if already computed
        existing = conn.execute(
            "SELECT id FROM stoploss_results WHERE call_id = ?", (call_id,)
        ).fetchone()
        if existing:
            done += 1
            progress.scanned = i
            progress.priced = done
            progress_cb(progress)
            continue

        try:
            # Load cached 1-minute candles from price_cache — NO API calls.
            # Try birdeye source first, then geckoterminal (any pool).
            candles = load_candles("birdeye", token_address, call_ts, end_ts)
            if not candles:
                pools = conn.execute(
                    "SELECT DISTINCT pool_address FROM price_cache "
                    "WHERE token_address = ? AND source = 'geckoterminal'",
                    (token_address,)
                ).fetchall()
                for (pool,) in pools:
                    candles = load_candles(pool, token_address, call_ts, end_ts)
                    if candles:
                        break

            if not candles:
                result = StoplossResult(
                    entry_price_usd=None, peak_price_usd=None, peak_timestamp=None,
                    peak_profit_pct=None, hit_stoploss=False, stoploss_timestamp=None,
                    is_win=False, status="unpriceable_loss", error="no cached candles",
                )
            else:
                result = score_candles_stoploss(
                    candles, call_ts, token_address,
                    win_multiplier=2.0, stoploss_pct=50.0,
                )

            # Store result
            conn.execute(
                """INSERT INTO stoploss_results
                (call_id, entry_price_usd, peak_price_usd, peak_timestamp,
                 peak_profit_pct, hit_stoploss, stoploss_timestamp,
                 is_win, status, error, computed_at)
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
                computed_at=datetime('now')""",
                (
                    call_id,
                    result.entry_price_usd,
                    result.peak_price_usd,
                    _iso(result.peak_timestamp) if result.peak_timestamp else None,
                    result.peak_profit_pct,
                    1 if result.hit_stoploss else 0,
                    _iso(result.stoploss_timestamp) if result.stoploss_timestamp else None,
                    1 if result.is_win else 0,
                    result.status,
                    result.error,
                ),
            )
            done += 1
        except Exception as e:
            log.exception("stoploss backtest failed for call %s", call_id)
            conn.execute(
                """INSERT INTO stoploss_results
                (call_id, status, error, computed_at)
                VALUES (?, 'unpriceable_loss', ?, datetime('now'))
                ON CONFLICT(call_id) DO UPDATE SET
                status='unpriceable_loss', error=?, computed_at=datetime('now')""",
                (call_id, str(e), str(e)),
            )
            errors += 1

        progress.scanned = i
        progress.priced = done
        progress.unpriceable = errors
        progress_cb(progress)

    progress.stage = "done"
    progress.message = f"stoploss backtest: {done} done, {errors} errors"
    progress_cb(progress)
    return progress


def init_stoploss_table() -> None:
    """Create the stoploss_results table if it doesn't exist."""
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stoploss_results (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id           INTEGER NOT NULL REFERENCES calls(id),
            entry_price_usd   REAL,
            peak_price_usd    REAL,
            peak_timestamp    TEXT,
            peak_profit_pct   REAL,
            hit_stoploss      INTEGER NOT NULL DEFAULT 0,
            stoploss_timestamp TEXT,
            is_win            INTEGER NOT NULL DEFAULT 0,
            status            TEXT NOT NULL DEFAULT 'pending',
            error             TEXT,
            computed_at       TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(call_id)
        )
    """)
