"""
Local read-only API for the Next.js frontend (KOLfi).

Built on Starlette + uvicorn (already installed) — no FastAPI/pydantic needed,
so zero new downloads. Reads the same SQLite DB (WAL) that the pipeline writes.

Run:
    .venv/bin/python -m api.server          # serves http://localhost:8000
Endpoints (all ?strategy=normal|stoploss, ?window=1d|7d|1m|3m|all):
    GET /api/channels                     -> cards for All/Hot/Consistent/New
    GET /api/leaderboard?strategy=        -> ranked channels
    GET /api/channels/{handle}?window=&strategy=  -> deep-dive header stats
    GET /api/channels/{handle}/buckets?window=&strategy= -> chart buckets
    GET /api/channels/{handle}/streak?strategy=   -> current consecutive wins
    GET /api/channels/{handle}/calls?window=      -> token-call grid data
"""
from __future__ import annotations

import json
from datetime import datetime

from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
import asyncio
import json

from db import init_db, get_connection
from analysis import windowed as W

init_db()


# ── startup background catch-up ─────────────────────────────────────────────
# If the machine/app was closed for a while, reconcile provisional 'live'
# calls the moment the API comes up: >7d rows get their FINAL frozen-spec
# verdict, <7d rows get fresh provisional verdicts. Daemon thread so serving
# starts immediately; WAL reads are never blocked by it.
import threading as _threading


from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(app):
    def _worker():
        try:
            from pipeline import mature_pending_calls, rescore_live_calls
            mature_pending_calls()
            rescore_live_calls()
        except Exception:
            import logging
            logging.getLogger(__name__).exception("startup catch-up failed")

    _threading.Thread(target=_worker, name="startup-catchup", daemon=True).start()
    yield


def _j(rows) -> JSONResponse:
    return JSONResponse(json.loads(json.dumps(rows, default=str)))


def _chain(request, default: str = "all") -> str | None:
    """?chain=sol|robinhood|eth|bsc|base|arc|all — 'all' returns None (no chain filter).

    On the legacy solana_tracker.db there is no chain column (every row is
    Solana), so any requested chain degrades to None — merged stats there
    ARE the per-chain stats. Keeps old API clients unbroken on both DBs.
    """
    v = request.query_params.get("chain", default)
    if v not in ("sol", "robinhood", "eth", "bsc", "base", "arc", "all"):
        v = default
    if v == "all" or not _unified():
        return None
    return v


_UNIFIED: bool | None = None


def _unified() -> bool:
    """True when the live DB is the unified kolfi schema (has calls.chain)."""
    global _UNIFIED
    if _UNIFIED is None:
        conn = get_connection()
        _UNIFIED = any(r["name"] == "chain"
                       for r in conn.execute("PRAGMA table_info(calls)").fetchall())
    return _UNIFIED


# ── /api/channels ─────────────────────────────────────────────────────────
def channels(request):
    strategy = request.query_params.get("strategy", "normal")
    window = request.query_params.get("window", "all")
    chain = _chain(request)
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, telegram_channel_id, username, title, created_at FROM channels ORDER BY title"
    ).fetchall()
    # In merged 'all' mode, tag each channel with which chains actually have
    # decided calls there, so the UI can badge rows SOL / RH / both.
    chains_map: dict = {}
    if chain is None and _unified():
        for r in conn.execute(
            "SELECT channel_id, chain FROM calls WHERE status IN ('win','loss') "
            "GROUP BY channel_id, chain"
        ).fetchall():
            chains_map.setdefault(r["channel_id"], []).append(r["chain"])
    out = []
    for r in rows:
        st = W.channel_stats_window(r["id"], window, strategy, chain=chain)
        streak = W.current_streak(r["id"], strategy, chain=chain)
        out.append({
            "channel_id": r["id"],
            "username": r["username"],
            "title": r["title"],
            "chain": chain or "all",
            "chains": chains_map.get(r["id"], []) if chain is None else None,
            "total_calls": st["total_calls"],
            "win_rate": st["win_rate"],
            "avg_peak_profit_pct": st["avg_peak_profit_pct"],
            "streak": streak,
            "created_at": r["created_at"],
        })
    return _j(out)


# ── /api/leaderboard ──────────────────────────────────────────────────────
def leaderboard(request):
    """Ranked channels using decided-only (win+loss) stats for the chosen
    strategy + window, so pending/unpriceable never pollute the denominator.
    Each row also carries the channel's best call (highest multiplier) in the
    window for the "top call" column."""
    strategy = request.query_params.get("strategy", "normal")
    window = request.query_params.get("window", "all")
    chain = _chain(request)
    conn = get_connection()
    since = W.window_since(window)
    rows = conn.execute(
        "SELECT id, username, title FROM channels ORDER BY title"
    ).fetchall()
    chains_map: dict = {}
    if chain is None and _unified():
        for r in conn.execute(
            "SELECT channel_id, chain FROM calls WHERE status IN ('win','loss') "
            "GROUP BY channel_id, chain"
        ).fetchall():
            chains_map.setdefault(r["channel_id"], []).append(r["chain"])
    out = []
    for r in rows:
        st = W.channel_stats_window(r["id"], window, strategy, chain=chain)
        if not st["total_calls"]:
            continue  # no decided calls yet for this strategy/window

        # Best call in window: highest achieved multiple among decided calls.
        # Unified schema: COALESCE(max_multiple, peak_multiple, 1+pp%) so 7d
        # rows (max_multiple) and legacy rows (peak_multiple OR only the
        # percent they carry) both rank — a win with no multiple field used
        # to drop out of the tier/best-call math entirely (X2 ≠ win rate bug).
        mult_col = ("COALESCE(max_multiple, peak_multiple, "
                    "1.0 + peak_profit_pct / 100.0)" if _unified()
                    else "peak_multiple")
        q = f"""
            SELECT token_symbol, {mult_col} AS best_mult FROM calls
            WHERE channel_id = ? AND status IN ('win','loss')
              AND {mult_col} IS NOT NULL
        """
        params = [r["id"]]
        if chain:
            q += " AND chain = ?"
            params.append(chain)
        if since:
            q += " AND call_timestamp >= ?"
            params.append(since.replace(microsecond=0).isoformat() + "Z")
        q += " ORDER BY best_mult DESC LIMIT 1"
        best = conn.execute(q, params).fetchone()

        out.append({
            "channel_id": r["id"],
            "channel_title": r["title"],
            "channel_username": r["username"],
            "chain": chain or "all",
            "chains": chains_map.get(r["id"], []) if chain is None else None,
            "total_calls": st["total_calls"],
            "wins": st["wins"],
            "win_rate": st["win_rate"],
            "avg_peak_profit_pct": st["avg_peak_profit_pct"],
            "top_call_roi": best["best_mult"] if best else None,
            "top_call_token": best["token_symbol"] if best else None,
        })
    out.sort(key=lambda d: (d["win_rate"] or 0, d["avg_peak_profit_pct"] or 0, d["total_calls"]), reverse=True)
    for i, d in enumerate(out, start=1):
        d["rank"] = i
    return _j(out)


# ── /api/channels/{handle} ────────────────────────────────────────────────
def _resolve(handle: str):
    conn = get_connection()
    row = conn.execute(
        "SELECT id, username, title FROM channels WHERE username = ? OR id = ?",
        (handle, handle if handle.isdigit() else -1),
    ).fetchone()
    if not row:
        # Fallback: match by title (case-insensitive) so handles work even when
        # the username column is null.
        row = conn.execute(
            "SELECT id, username, title FROM channels WHERE lower(title) = lower(?)",
            (handle,),
        ).fetchone()
    return row


def channel_detail(request):
    handle = request.path_params["handle"]
    window = request.query_params.get("window", "all")
    strategy = request.query_params.get("strategy", "normal")
    chain = _chain(request, default="all")
    row = _resolve(handle)
    if not row:
        return JSONResponse({"error": "channel not found"}, status_code=404)
    st = W.channel_stats_window(row["id"], window, strategy, chain=chain)
    return _j({**dict(row), **st, "window": window, "strategy": strategy,
               "chain": chain or "all"})


def channel_buckets(request):
    handle = request.path_params["handle"]
    window = request.query_params.get("window", "all")
    strategy = request.query_params.get("strategy", "normal")
    chain = _chain(request, default="all")
    row = _resolve(handle)
    if not row:
        return JSONResponse({"error": "channel not found"}, status_code=404)
    return _j(W.channel_buckets(row["id"], window, strategy, chain=chain))


def channel_streak(request):
    handle = request.path_params["handle"]
    strategy = request.query_params.get("strategy", "normal")
    chain = _chain(request, default="all")
    row = _resolve(handle)
    if not row:
        return JSONResponse({"error": "channel not found"}, status_code=404)
    return _j({"streak": W.current_streak(row["id"], strategy, chain=chain)})


def channel_calls(request):
    handle = request.path_params["handle"]
    window = request.query_params.get("window", "all")
    chain = _chain(request, default="all")
    row = _resolve(handle)
    if not row:
        return JSONResponse({"error": "channel not found"}, status_code=404)
    since = W.window_since(window)
    conn = get_connection()
    unified = _unified()
    extra = (", chain, score_state, max_multiple, which_threshold_first, "
             "api_requests_used,\n"
             "               granular_analysis_required, max_drawdown_pct"
             if unified else "")
    q = f"""
        SELECT id, token_address, token_symbol, token_name, call_timestamp,
               entry_price_usd, peak_price_usd, peak_profit_pct, is_win, status{extra}
        FROM calls WHERE channel_id = ? AND status IN ('win','loss')
    """
    params = [row["id"]]
    if unified and chain:
        q += " AND chain = ?"
        params.append(chain)
    if since:
        q += " AND call_timestamp >= ?"
        params.append(since.replace(microsecond=0).isoformat() + "Z")
    q += " ORDER BY call_timestamp DESC"
    rows = conn.execute(q, params).fetchall()
    out = []
    for r in rows:
        entry = r["entry_price_usd"]
        peak = r["peak_price_usd"]
        d = dict(r)
        # Unified 7d rows may carry a more precise max_multiple; legacy rows
        # fall back to peak/entry division. which_first renamed to 'chain-first'
        # naming from the prototype (which_threshold_first).
        best_mult = d.get("max_multiple") if unified else None
        d["multiplier"] = best_mult or ((peak / entry) if entry and peak else None)
        if unified:
            d["which_first"] = d.pop("which_threshold_first", None)
            d["granular"] = d.pop("granular_analysis_required", None)
        out.append(d)
    return _j(out)


# ── /api/channels/{handle}/tiers ────────────────────────────────────────────
def channel_tiers(request):
    """Cumulative performance-tier counts for the ranking meter.

    ?chain=sol|robinhood|all (default sol)  ?strategy=normal|stoploss
    ?window=1d|7d|1m|3m|all                ?days=1|3|7|30 (wins over window)
    """
    handle = request.path_params["handle"]
    strategy = request.query_params.get("strategy", "normal")
    window = request.query_params.get("window", "all")
    chain = _chain(request)
    row = _resolve(handle)
    if not row:
        return JSONResponse({"error": "channel not found"}, status_code=404)

    from datetime import datetime, timedelta, timezone
    from analysis.tiers import tier_counts
    days_raw = request.query_params.get("days")
    since = None
    if days_raw:
        try:
            since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=int(days_raw))
        except ValueError:
            return JSONResponse({"error": "days must be an integer"}, status_code=400)
    res = tier_counts(row["id"], window, strategy, chain=chain, since=since)

    # Engine-effort stats over the same population (7d rows only, if present).
    granular_calls = None
    avg_api_requests = None
    if _unified():
        conn = get_connection()
        q = ("SELECT COUNT(*) n, SUM(granular_analysis_required) g, "
             "AVG(api_requests_used) a FROM calls cal "
             "WHERE cal.channel_id = ? AND cal.engine = '7d' "
             "AND cal.status IN ('win','loss')")
        params: list = [row["id"]]
        if chain:
            q += " AND cal.chain = ?"
            params.append(chain)
        if since:
            q += " AND cal.call_timestamp >= ?"
            params.append(since.replace(microsecond=0).isoformat() + "Z")
        r = conn.execute(q, params).fetchone()
        if r and r["n"]:
            granular_calls = r["g"] or 0
            avg_api_requests = round(r["a"], 2) if r["a"] is not None else None

    return _j({
        "scope": {"chain": chain or "all", "strategy": strategy,
                  "window": window, "days": int(days_raw) if days_raw else None,
                  "since": since.replace(microsecond=0).isoformat() + "Z" if since else None},
        "total_decided": res["total_decided"],
        "wins": res["wins"],
        "win_rate": res["win_rate"],
        "tiers": [{"tier": t["label"], "count": t["count"], "pct": t["pct"]}
                  for t in res["tiers"]],
        "granular_calls": granular_calls,
        "avg_api_requests": avg_api_requests,
    })


# ── /api/tokens ───────────────────────────────────────────────────────────
def tokens(request):
    """Token-level aggregation for the Tokens page: every decided call grouped
    by token, sorted by the number of DISTINCT channels that called the token
    (desc). Each token carries its call entries in chronological order (order
    of call) with the calling channel and the multiplier that call achieved."""
    window = request.query_params.get("window", "all")
    chain = _chain(request)
    conn = get_connection()
    since = W.window_since(window)

    q = """
        SELECT c.id, c.token_address, c.token_symbol, c.token_name,
               c.channel_id, c.call_timestamp, c.entry_price_usd,
               c.peak_price_usd, c.is_win,
               ch.title AS channel_title, ch.username AS channel_username
        FROM calls c
        JOIN channels ch ON ch.id = c.channel_id
        WHERE c.status IN ('win', 'loss')
    """
    params = []
    if chain:
        q += " AND c.chain = ?"
        params.append(chain)
    if since:
        q += " AND c.call_timestamp >= ?"
        params.append(since.replace(microsecond=0).isoformat() + "Z")
    q += " ORDER BY c.call_timestamp ASC"
    rows = conn.execute(q, params).fetchall()

    grouped: dict[str, dict] = {}
    for r in rows:
        addr = r["token_address"]
        g = grouped.setdefault(addr, {
            "token_address": addr,
            "token_symbol": r["token_symbol"],
            "token_name": r["token_name"],
            "channels": set(),
            "calls_count": 0,
            "call_entries": [],
        })
        if r["token_symbol"] and not g["token_symbol"]:
            g["token_symbol"] = r["token_symbol"]
        if r["token_name"] and not g["token_name"]:
            g["token_name"] = r["token_name"]
        g["channels"].add(r["channel_id"])
        entry = r["entry_price_usd"]
        peak = r["peak_price_usd"]
        g["calls_count"] += 1
        # Rows arrive ASC by call_timestamp, so call_entries stay in order of call.
        g["call_entries"].append({
            "call_id": r["id"],
            "channel_title": r["channel_title"],
            "channel_username": r["channel_username"],
            "entry_price_usd": entry,
            "peak_price_usd": peak,
            "call_timestamp": r["call_timestamp"] or "",
            "is_win": bool(r["is_win"]),
            "multiplier": (peak / entry) if entry and peak else None,
        })

    out = []
    for g in grouped.values():
        out.append({
            "token_address": g["token_address"],
            "token_symbol": g["token_symbol"] or g["token_address"][:6],
            "token_name": g["token_name"],
            "channels_count": len(g["channels"]),
            "calls_count": g["calls_count"],
            "call_entries": g["call_entries"],
        })
    # Most-called-across-channels first; tie-break by newest call activity.
    out.sort(key=lambda t: (
        t["channels_count"],
        t["call_entries"][-1]["call_timestamp"] or "",
    ), reverse=True)
    return _j(out)


# ── /api/fetch-stream ────────────────────────────────────────────────────────
async def fetch_stream(request: Request):
    """Stream progress updates for channel fetching via SSE.
    POST body: { "channel_ids": [12, 13], "days": 7? }

    Window: the STANDARD 5-month anchor (preset_window('5m') = 1st of the
    month 5 months back .. now) unless the client passes an explicit 'days'
    override. Calls older than 7d get the frozen full-window verdict;
    younger ones are scored live.

    No chain parameter needed: run_backfill parses Solana mints AND
    Robinhood 0x addresses from every message in one pass (Workstream D)
    and prices each row via the PRICING_ENGINE dispatcher with its chain.
    """
    try:
        body = await request.json()
        channel_ids = body.get("channel_ids", [])
        days = body.get("days")  # optional override; default = 5m preset
        
        if not channel_ids:
            return JSONResponse({"success": False, "message": "No channels selected"}, status_code=400)
        
        from datetime import datetime, timedelta, timezone
        from pipeline import run_backfill, Progress
        from ingestion.telegram_guard import request_telegram_yield
        
        conn = get_connection()
        
        async def event_generator():
            # A user fetch outranks every opportunistic refresh: set the yield
            # flag so any concurrent refresh releases the Telegram session
            # within one message batch and retries after us. Cleared when this
            # stream ends (including client disconnect — the generator's
            # finally runs on cancellation).
            request_telegram_yield(True)
            try:
                async for chunk in _fetch_stream_inner(channel_ids, days, conn):
                    yield chunk
            finally:
                request_telegram_yield(False)
        
        return StreamingResponse(event_generator(), media_type="text/event-stream")
    
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


async def _fetch_stream_inner(channel_ids, days, conn):
    """Body of fetch_stream; split out so the yield-flag set/clear wraps it
    in one place."""
    from datetime import datetime, timedelta, timezone
    from pipeline import run_backfill, Progress

    for channel_id in channel_ids:
        # Try primary key first, then telegram_channel_id
        row = conn.execute(
            "SELECT id, telegram_channel_id, username, title FROM channels WHERE id = ?",
            (channel_id,)
        ).fetchone()

        if not row:
            row = conn.execute(
                "SELECT id, telegram_channel_id, username, title FROM channels WHERE telegram_channel_id = ?",
                (channel_id,)
            ).fetchone()

        if not row:
            yield f"data: {json.dumps({'channel_id': channel_id, 'status': 'error', 'message': 'Channel not found'})}\n\n"
            continue

        # Send start event
        yield f"data: {json.dumps({'channel_id': row['id'], 'status': 'start', 'title': row['title']})}\n\n"

        # Calculate window: the STANDARD 5-month anchor (same start
        # as the historical backfill preset), unless the client sends
        # an explicit days override.
        if days:
            window_end = datetime.now(timezone.utc).replace(tzinfo=None)
            window_start = window_end - timedelta(days=int(days))
        else:
            from pipeline import preset_window
            window_start, window_end = preset_window("5m")

        # Create progress queue
        progress_queue = asyncio.Queue()

        def progress_cb(progress: Progress):
            # This runs in a sync context, so we need to use asyncio.run_coroutine_threadsafe
            asyncio.run_coroutine_threadsafe(
                progress_queue.put({
                    'channel_id': row['id'],
                    'status': 'progress',
                    'stage': progress.stage,
                    'scanned': progress.scanned,
                    'found': progress.found,
                    'priced': progress.priced,
                    'unpriceable': progress.unpriceable,
                    'immature': progress.immature,
                    'live': progress.live,
                    'waiting': progress.waiting,
                    'total_calls': progress.total_calls,
                    'message': progress.message,
                }),
                loop
            )

        # Run backfill in a thread
        loop = asyncio.get_event_loop()

        # Telethon ref: @username if public, else the numeric Telegram id
        # (NOT the DB primary key — that resolves to nothing in Telegram).
        channel_ref = f"@{row['username']}" if row["username"] else str(row["telegram_channel_id"])

        def run_sync():
            # Background rescore yields to user-initiated backfills.
            from pipeline import pause_rescoring
            try:
                with pause_rescoring():
                    result = run_backfill(
                        channel_ref=channel_ref,
                        window_start=window_start.replace(tzinfo=None),
                        window_end=window_end.replace(tzinfo=None),
                        title=row["title"],
                        username=row["username"],
                        progress_cb=progress_cb,
                        birdeye_rescue=True,  # initial fetch: rescue allowed
                    )
                return result
            except Exception as e:
                return e

        # Start backfill in thread
        task = loop.run_in_executor(None, run_sync)

        # Stream progress updates
        while not task.done():
            try:
                update = await asyncio.wait_for(progress_queue.get(), timeout=0.5)
                yield f"data: {json.dumps(update)}\n\n"
            except asyncio.TimeoutError:
                continue

        # Get final result
        result = await task
        if isinstance(result, Exception):
            yield f"data: {json.dumps({'channel_id': row['id'], 'status': 'error', 'message': str(result)})}\n\n"
        else:
            yield f"data: {json.dumps({'channel_id': row['id'], 'status': 'done', 'total_calls': result.total_calls})}\n\n"

    # Before finishing: refresh every provisional 'live' verdict
    # across the DB (calls whose 7d window is still open, plus young
    # rows that were deferred before live scoring existed). Cheap by
    # design: closed candles come from price_cache.
    from pipeline import rescore_live_calls
    yield f"data: {json.dumps({'status': 'rescore_start'})}\n\n"
    loop2 = asyncio.get_event_loop()
    # Stream real per-call counters (see refresh_stream note — silence reads
    # as a hang on GT's ~5/min pace).
    _rq = asyncio.Queue()

    def _rescore_cb(progress):
        asyncio.run_coroutine_threadsafe(
            _rq.put({'status': 'progress', 'stage': 'rescore',
                     'scanned': progress.scanned,
                     'total_calls': progress.total_calls,
                     'priced': progress.priced,
                     'message': progress.message}),
            loop2
        )

    _rt = loop2.run_in_executor(None, rescore_live_calls, _rescore_cb)
    while not _rt.done():
        try:
            _u = await asyncio.wait_for(_rq.get(), timeout=0.5)
            yield f"data: {json.dumps(_u)}\n\n"
        except asyncio.TimeoutError:
            continue
    n_live = await _rt
    yield f"data: {json.dumps({'status': 'rescore_done', 'updated': n_live})}\n\n"

    # Send completion
    yield f"data: {json.dumps({'status': 'complete'})}\n\n"


# ── /api/refresh-stream ──────────────────────────────────────────────────────
async def refresh_stream(request: Request):
    """Stream progress updates for refreshing all channels from their SCAN
    CHECKPOINT (channels.last_scanned_at — the newest point their messages
    were walked through on a completed run, active and quiet channels alike)
    to now; falls back to last stored call, then 7 days, only for channels
    that have never completed a scan.

    BOOT GATE (user policy 2026-09-22): a boot refresh (no body) runs at
    most once per 24h. If a completed run is younger, the stream instantly
    emits 'skipped' and does nothing. An interrupted run RESUMES from the
    channels that never completed (see ingestion/boot_refresh.py). The
    manual 'Refresh All' button posts {force: true} and always runs; it
    does not consume or extend the boot day.
    POST body: {} (boot) | {"force": true} (manual Refresh All)
    """
    try:
        from datetime import datetime, timedelta, timezone
        from pipeline import run_backfill, Progress
        
        conn = get_connection()
        
        # ── daily boot gate ────────────────────────────────────────────────
        force = False
        try:
            body = await request.json()
            force = bool(body.get("force"))
        except Exception:
            pass
        from ingestion import boot_refresh as BR
        BR.ensure_gate_table()
        due, _prior_token = BR.boot_refresh_due()
        if not force and not due:
            hrs = BR.hours_since_last_finished()
            msg = (f"Already updated — channels auto-refresh once a day"
                   + (f" (last completed {hrs:.0f}h ago)" if hrs is not None else ""))
            async def _skip():
                yield f"data: {json.dumps({'status': 'skipped', 'message': msg})}\n\n"
                yield f"data: {json.dumps({'status': 'complete', 'skipped': True})}\n\n"
            return StreamingResponse(_skip(), media_type="text/event-stream")
        # NOTE: the run token is claimed INSIDE event_generator (first
        # consumption), not here — a request that dies before streaming
        # (client disconnect, 400 below) must never leave a half-open run.
        
        # Scan checkpoint (last_scanned_at) is the primary anchor: the newest
        # point messages were demonstrably walked through. Falling back to
        # MAX(call.timestamp) re-scanned a quiet channel's whole silent
        # backlog on every boot (user report 2026-09-16).
        channels = conn.execute("""
            SELECT c.id, c.telegram_channel_id, c.username, c.title,
                   c.last_scanned_at,
                   MAX(cal.call_timestamp) as last_call_ts
            FROM channels c
            LEFT JOIN calls cal ON cal.channel_id = c.id
            GROUP BY c.id
            ORDER BY c.title
        """).fetchall()
        
        if not channels:
            return JSONResponse({"success": False, "message": "No channels found"}, status_code=400)
        
        async def _one_channel(row, out, opportunistic=True):
            """One channel of an opportunistic refresh: emits its SSE events
            and records the outcome in out[0] (done | error | yielded)."""
            yield f"data: {json.dumps({'channel_id': row['id'], 'status': 'start', 'title': row['title']})}\n\n"

            # Window: from the SCAN CHECKPOINT (last_scanned_at — the newest
            # point messages were walked through, even if they held no call)
            # to now. Fall back to the last stored call, then to 7 days —
            # anchoring only at MAX(call.timestamp) re-scanned a quiet
            # channel's entire silent backlog on every boot (the 'since 26
            # Aug' progress bars the user reported 2026-09-16).
            window_end = datetime.now(timezone.utc)
            anchor = None
            for raw in (row['last_scanned_at'], row['last_call_ts']):
                if not raw:
                    continue
                # Legacy rows stored NAIVE ISO strings (channel 12/13-era:
                # '2026-08-10T20:12:29' — always UTC), unified rows store
                # 'Z'-suffixed aware ones. Mixing the two made
                # (aware - naive) raise TypeError and killed the whole
                # boot-refresh stream at that channel.
                ts = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                anchor = ts if anchor is None else max(anchor, ts)
                break  # first non-null wins (checkpoint preferred over call)
            window_start = anchor if anchor else window_end - timedelta(days=7)

            # Skip if window is too small (less than 1 hour)
            if (window_end - window_start).total_seconds() < 3600:
                out[0] = 'done'
                yield f"data: {json.dumps({'channel_id': row['id'], 'status': 'done', 'total_calls': 0, 'message': 'Already up to date'})}\n\n"
                return

            # Create progress queue
            progress_queue = asyncio.Queue()
            loop = asyncio.get_event_loop()

            def progress_cb(progress: Progress):
                asyncio.run_coroutine_threadsafe(
                    progress_queue.put({
                        'channel_id': row['id'],
                        'status': 'progress',
                        'stage': progress.stage,
                        'scanned': progress.scanned,
                        'found': progress.found,
                        'priced': progress.priced,
                        'unpriceable': progress.unpriceable,
                        'immature': progress.immature,
                        'live': progress.live,
                        'waiting': progress.waiting,
                        'total_calls': progress.total_calls,
                        'message': progress.message,
                    }),
                    loop
                )

            # Telethon ref: @username if public, else the numeric Telegram id
            channel_ref = f"@{row['username']}" if row["username"] else str(row["telegram_channel_id"])

            def run_sync():
                # Background rescore yields to user-initiated backfills.
                from pipeline import pause_rescoring
                try:
                    with pause_rescoring():
                        return run_backfill(
                            channel_ref=channel_ref,
                            window_start=window_start.replace(tzinfo=None),
                            window_end=window_end.replace(tzinfo=None),
                            title=row["title"],
                            username=row["username"],
                            progress_cb=progress_cb,
                            opportunistic=opportunistic,
                            birdeye_rescue=False,
                        )
                except Exception as e:
                    return e

            # Start backfill in thread
            task = loop.run_in_executor(None, run_sync)

            # Stream progress updates
            while not task.done():
                try:
                    update = await asyncio.wait_for(progress_queue.get(), timeout=0.5)
                    yield f"data: {json.dumps(update)}\n\n"
                except asyncio.TimeoutError:
                    continue

            # Get final result
            result = await task

            if isinstance(result, Exception):
                out[0] = 'error'
                yield f"data: {json.dumps({'channel_id': row['id'], 'status': 'error', 'message': str(result)})}\n\n"
            elif getattr(result, 'stage', '') == 'yielded':
                # A user fetch took (or wanted) the Telegram session. Keep the
                # card OPEN (progress, not done) — the retry lane below (or
                # the next refresh) finishes this channel.
                out[0] = 'yielded'
                yield f"data: {json.dumps({'channel_id': row['id'], 'status': 'progress', 'stage': 'yielded', 'message': 'paused — a fetch is using Telegram; will retry right after'})}\n\n"
            else:
                out[0] = 'done'
                yield f"data: {json.dumps({'channel_id': row['id'], 'status': 'done', 'total_calls': result.total_calls})}\n\n"

        async def event_generator():
            # Claim the run token now that streaming actually begins (boot:
            # new or resume of an interrupted run; manual force: token None
            # -> every bookkeeping mark is a no-op).
            gate_token = None if force else (BR.mark_boot_started() or _prior_token)
            # Gate resume: a boot run continuing an interrupted one skips
            # every channel already recorded done for this token (manual
            # force-runs carry token=None -> empty set, full queue).
            done_ids = BR.completed_channel_ids(gate_token)
            remaining = [r for r in channels if r["id"] not in done_ids]
            # Announce the full queue up front so the UI can render every
            # channel as 'queued' immediately, then flip them to 'running'
            # one-by-one as this loop reaches each (sequential execution).
            # Resumed channels are folded straight into 'done' so the n/total
            # counter reflects real progress without pretending to rescan.
            queue_payload = [
                {"channel_id": r["id"], "title": r["title"],
                 "status": "done" if r["id"] in done_ids else "queued"}
                for r in channels
            ]
            yield f"data: {json.dumps({'status': 'queue', 'channels': queue_payload})}\n\n"
            if done_ids:
                yield f"data: {json.dumps({'status': 'resumed', 'message': f'resuming interrupted refresh — {len(done_ids)} channel(s) already done, {len(remaining)} to go'})}\n\n"
            deferred = []
            had_error = False
            for row in remaining:
                out = [None]
                async for chunk in _one_channel(row, out, opportunistic=True):
                    yield chunk
                if out[0] == 'yielded':
                    deferred.append(row)
                elif out[0] == 'error':
                    had_error = True
                else:
                    # done OR 'Already up to date' — either way this run
                    # walked the channel fully; it won't need rescanning if
                    # the boot run is interrupted right after.
                    BR.mark_channel_done(gate_token, row["id"])

            # Retry lane: channels that paused for a user fetch get a second
            # pass once the fetch has released the Telegram session. Their
            # scans no longer yield, so this blocks only on the session lock
            # — seconds per window. The refresh waiting for a fetch is fine
            # (it is the opportunistic job); NEVER the reverse.
            if deferred:
                from ingestion.telegram_guard import should_yield_fetch
                waited = False
                while should_yield_fetch():
                    if not waited:
                        # Keep the feed honest while we stand down.
                        for row in deferred:
                            yield f"data: {json.dumps({'channel_id': row['id'], 'status': 'progress', 'message': 'waiting for your fetch to finish…'})}\n\n"
                        waited = True
                    await asyncio.sleep(2)
                for row in deferred:
                    out2: list = [None]
                    async for chunk in _one_channel(row, out2, opportunistic=False):
                        yield chunk
                    if out2[0] == 'error':
                        had_error = True
                    elif out2[0] == 'done':
                        BR.mark_channel_done(gate_token, row["id"])
            
            # Before finishing: refresh every provisional 'live' verdict
            # across the DB (calls whose 7d window is still open, plus young
            # rows that were deferred before live scoring existed). Cheap by
            # design: closed candles come from price_cache.
            from pipeline import rescore_live_calls
            yield f"data: {json.dumps({'status': 'rescore_start'})}\n\n"
            loop2 = asyncio.get_event_loop()
            # Stream the rescore's real per-call counters into the feed —
            # without a callback the footer sat on one silent line for the
            # whole pass (GT's ~5/min bucket makes ~40 live calls take many
            # minutes, and users reasonably read silence as a hang).
            _rq = asyncio.Queue()

            def _rescore_cb(progress):
                asyncio.run_coroutine_threadsafe(
                    _rq.put({'status': 'progress', 'stage': 'rescore',
                             'scanned': progress.scanned,
                             'total_calls': progress.total_calls,
                             'priced': progress.priced,
                             'message': progress.message}),
                    loop2
                )

            _rt = loop2.run_in_executor(None, rescore_live_calls, _rescore_cb)
            while not _rt.done():
                try:
                    _u = await asyncio.wait_for(_rq.get(), timeout=0.5)
                    yield f"data: {json.dumps(_u)}\n\n"
                except asyncio.TimeoutError:
                    continue
            n_live = await _rt
            yield f"data: {json.dumps({'status': 'rescore_done', 'updated': n_live})}\n\n"

            # Gate close: the 24h boot clock starts only on a FULLY clean
            # run. With errored channels finished_at stays NULL, so the
            # next page open resumes the same token and retries ONLY the
            # failures (every other channel is already recorded done).
            # Manual force runs carry token None -> this is a no-op.
            BR.mark_boot_finished(gate_token, clean=not had_error)

            # Send completion
            yield f"data: {json.dumps({'status': 'complete'})}\n\n"
        
        return StreamingResponse(event_generator(), media_type="text/event-stream")
    
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


# ── /api/fetch ───────────────────────────────────────────────────────────
async def fetch_channels(request: Request):
    """Trigger a backfill for selected channels.
    POST body: { "channel_ids": [12, 13], "days": 7? }
    No 'days' => standard 5-month anchor (same as fetch-stream).
    """
    try:
        body = await request.json()
        channel_ids = body.get("channel_ids", [])
        days = body.get("days")  # optional override; default = 5m preset

        if not channel_ids:
            return JSONResponse({"success": False, "message": "No channels selected"}, status_code=400)

        from datetime import datetime, timedelta, timezone
        from pipeline import run_backfill, preset_window

        conn = get_connection()
        results = []
        
        for channel_id in channel_ids:
            # Try primary key first
            row = conn.execute(
                "SELECT id, telegram_channel_id, username, title FROM channels WHERE id = ?",
                (channel_id,)
            ).fetchone()
            
            # Fallback: try telegram_channel_id
            if not row:
                row = conn.execute(
                    "SELECT id, telegram_channel_id, username, title FROM channels WHERE telegram_channel_id = ?",
                    (channel_id,)
                ).fetchone()
            
            if not row:
                results.append({"channel_id": channel_id, "success": False, "message": "Channel not found"})
                continue
            
            # Calculate window: standard 5-month anchor unless 'days' given
            if days:
                window_end = datetime.now(timezone.utc).replace(tzinfo=None)
                window_start = window_end - timedelta(days=int(days))
            else:
                window_start, window_end = preset_window("5m")
            
            # Telethon ref: @username if public, else the numeric Telegram id
            channel_ref = f"@{row['username']}" if row["username"] else str(row["telegram_channel_id"])
            
            try:
                # Run backfill for this channel (background rescore yields)
                from pipeline import pause_rescoring
                with pause_rescoring():
                    progress = run_backfill(
                        channel_ref=channel_ref,
                        window_start=window_start.replace(tzinfo=None),
                        window_end=window_end.replace(tzinfo=None),
                        title=row["title"],
                        username=row["username"],
                        birdeye_rescue=True,  # initial fetch: rescue allowed
                    )
                results.append({
                    "channel_id": channel_id,
                    "success": True,
                    "message": f"Fetched {progress.total_calls} calls",
                })
            except Exception as e:
                results.append({
                    "channel_id": channel_id,
                    "success": False,
                    "message": str(e),
                })
        
        return _j({"success": True, "results": results})
    
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


# ── /api/telegram-channels ───────────────────────────────────────────────
def telegram_channels(request):
    """List all Telegram channels/groups the user is a member of.
    Returns channels not yet in the DB, plus existing ones.
    """
    try:
        from ingestion.telethon_fetcher import fetch_my_dialogs_sync
        
        # Get all Telegram channels user is in
        telegram_channels = fetch_my_dialogs_sync()
        
        # Get existing channels in DB
        conn = get_connection()
        # One shared matcher for every entry point (see pipeline.find_channel_row
        # and the 2026-09-17 / 2026-09-20 phantom-duplicate incidents: Telegram
        # entity ids drift TRANSIENTLY, so id-only matching mints duplicates).
        from pipeline import find_channel_row
        # Build response
        out = []
        for tc in telegram_channels:
            tg_id = tc["id"]
            db_row = find_channel_row(conn, tg_id, tc.get("username"), tc.get("title"))
            is_in_db = db_row is not None
            
            out.append({
                "telegram_id": tg_id,
                "title": tc["title"],
                "username": tc["username"],
                "type": tc["type"],
                "in_database": is_in_db,
                "db_id": db_row["id"] if is_in_db else None,
            })
        
        # Sort: not-in-db first, then alphabetically
        out.sort(key=lambda x: (x["in_database"], x["title"].lower()))
        
        return _j(out)
    
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── /api/add-channels ────────────────────────────────────────────────────
async def add_channels(request: Request):
    """Add new channels to the database.
    POST body: { "channels": [{ "telegram_id": 123, "title": "...", "username": "..." }] }
    """
    try:
        body = await request.json()
        channels = body.get("channels", [])
        
        if not channels:
            return JSONResponse({"success": False, "message": "No channels provided"}, status_code=400)
        
        conn = get_connection()
        added = []
        
        for ch in channels:
            telegram_id = ch.get("telegram_id")
            title = ch.get("title")
            username = ch.get("username")
            
            if not telegram_id or not title:
                continue
            
            # Check if already exists via the shared stable-key cascade
            # (id -> username -> title-for-keyless). The 09-17/09-20 id drifts
            # made channels look new; matching by id alone minted phantoms.
            from pipeline import find_channel_row
            existing = find_channel_row(conn, telegram_id, username, title)
            
            if existing:
                added.append({"telegram_id": telegram_id, "db_id": existing["id"], "status": "exists"})
                continue
            
            # Insert new channel with placeholder window (run_backfill updates via ensure_channel)
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            conn.execute(
                """INSERT INTO channels (telegram_channel_id, title, username, window_start, window_end)
                   VALUES (?, ?, ?, ?, ?)""",
                (telegram_id, title, username, now, now)
            )
            
            # Get the new ID
            new_id = conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
            added.append({"telegram_id": telegram_id, "db_id": new_id, "status": "added"})
        
        return _j({"success": True, "added": added})
    
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


app_routes = [
    Route("/api/channels", channels),
    Route("/api/leaderboard", leaderboard),
    Route("/api/tokens", tokens),
    Route("/api/channels/{handle}", channel_detail),
    Route("/api/channels/{handle}/buckets", channel_buckets),
    Route("/api/channels/{handle}/streak", channel_streak),
    Route("/api/channels/{handle}/calls", channel_calls),
    Route("/api/channels/{handle}/tiers", channel_tiers),
    Route("/api/fetch-stream", fetch_stream, methods=["POST"]),
    Route("/api/refresh-stream", refresh_stream, methods=["POST"]),
    Route("/api/fetch", fetch_channels, methods=["POST"]),
    Route("/api/telegram-channels", telegram_channels),
    Route("/api/add-channels", add_channels, methods=["POST"]),
]

# AI chat is an optional surface: if this import ever fails the whole API
# still serves the rest of the app (the module itself imports nothing heavy
# at top level — requests/json/std only).
try:
    from api.aichat import ai_chat
    app_routes.append(Route("/api/ai-chat", ai_chat, methods=["POST"]))
except Exception:  # pragma: no cover - defensive per user's reliability rule
    import logging as _log
    _log.getLogger(__name__).exception("AI chat route disabled (import failed)")

app = Starlette(routes=app_routes, lifespan=_lifespan)

# The Next.js dev/prod server runs on :3000 and the browser fetches these
# endpoints cross-origin, so CORS must be enabled for every frontend call.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)