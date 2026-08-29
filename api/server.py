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


def _j(rows) -> JSONResponse:
    return JSONResponse(json.loads(json.dumps(rows, default=str)))


# ── /api/channels ─────────────────────────────────────────────────────────
def channels(request):
    strategy = request.query_params.get("strategy", "normal")
    window = request.query_params.get("window", "all")
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, telegram_channel_id, username, title, created_at FROM channels ORDER BY title"
    ).fetchall()
    out = []
    for r in rows:
        st = W.channel_stats_window(r["id"], window, strategy)
        streak = W.current_streak(r["id"], strategy)
        out.append({
            "channel_id": r["id"],
            "username": r["username"],
            "title": r["title"],
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
    conn = get_connection()
    since = W.window_since(window)
    rows = conn.execute(
        "SELECT id, username, title FROM channels ORDER BY title"
    ).fetchall()
    out = []
    for r in rows:
        st = W.channel_stats_window(r["id"], window, strategy)
        if not st["total_calls"]:
            continue  # no decided calls yet for this strategy/window

        # Best call in window: highest peak_multiple among decided calls.
        q = """
            SELECT token_symbol, peak_multiple FROM calls
            WHERE channel_id = ? AND status IN ('win','loss') AND peak_multiple IS NOT NULL
        """
        params = [r["id"]]
        if since:
            q += " AND call_timestamp >= ?"
            params.append(since.replace(microsecond=0).isoformat() + "Z")
        q += " ORDER BY peak_multiple DESC LIMIT 1"
        best = conn.execute(q, params).fetchone()

        out.append({
            "channel_id": r["id"],
            "channel_title": r["title"],
            "channel_username": r["username"],
            "total_calls": st["total_calls"],
            "wins": st["wins"],
            "win_rate": st["win_rate"],
            "avg_peak_profit_pct": st["avg_peak_profit_pct"],
            "top_call_roi": best["peak_multiple"] if best else None,
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
    row = _resolve(handle)
    if not row:
        return JSONResponse({"error": "channel not found"}, status_code=404)
    st = W.channel_stats_window(row["id"], window, strategy)
    return _j({**dict(row), **st, "window": window, "strategy": strategy})


def channel_buckets(request):
    handle = request.path_params["handle"]
    window = request.query_params.get("window", "all")
    strategy = request.query_params.get("strategy", "normal")
    row = _resolve(handle)
    if not row:
        return JSONResponse({"error": "channel not found"}, status_code=404)
    return _j(W.channel_buckets(row["id"], window, strategy))


def channel_streak(request):
    handle = request.path_params["handle"]
    strategy = request.query_params.get("strategy", "normal")
    row = _resolve(handle)
    if not row:
        return JSONResponse({"error": "channel not found"}, status_code=404)
    return _j({"streak": W.current_streak(row["id"], strategy)})


def channel_calls(request):
    handle = request.path_params["handle"]
    window = request.query_params.get("window", "all")
    row = _resolve(handle)
    if not row:
        return JSONResponse({"error": "channel not found"}, status_code=404)
    since = W.window_since(window)
    conn = get_connection()
    q = """
        SELECT id, token_address, token_symbol, token_name, call_timestamp,
               entry_price_usd, peak_price_usd, peak_profit_pct, is_win, status
        FROM calls WHERE channel_id = ? AND status IN ('win','loss')
    """
    params = [row["id"]]
    if since:
        q += " AND call_timestamp >= ?"
        params.append(since.replace(microsecond=0).isoformat() + "Z")
    q += " ORDER BY call_timestamp DESC"
    rows = conn.execute(q, params).fetchall()
    out = []
    for r in rows:
        entry = r["entry_price_usd"]
        peak = r["peak_price_usd"]
        out.append({
            **dict(r),
            "multiplier": (peak / entry) if entry and peak else None,
        })
    return _j(out)


# ── /api/tokens ───────────────────────────────────────────────────────────
def tokens(request):
    """Token-level aggregation for the Tokens page: every decided call grouped
    by token, sorted by the number of DISTINCT channels that called the token
    (desc). Each token carries its call entries in chronological order (order
    of call) with the calling channel and the multiplier that call achieved."""
    window = request.query_params.get("window", "all")
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
    POST body: { "channel_ids": [12, 13], "days": 7 }
    """
    try:
        body = await request.json()
        channel_ids = body.get("channel_ids", [])
        days = body.get("days", 7)
        
        if not channel_ids:
            return JSONResponse({"success": False, "message": "No channels selected"}, status_code=400)
        
        from datetime import datetime, timedelta, timezone
        from pipeline import run_backfill, Progress
        
        conn = get_connection()
        
        async def event_generator():
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
            
                # Calculate window
                window_end = datetime.now(timezone.utc)
                window_start = window_end - timedelta(days=days)
            
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
                    try:
                        result = run_backfill(
                            channel_ref=channel_ref,
                            window_start=window_start.replace(tzinfo=None),
                            window_end=window_end.replace(tzinfo=None),
                            title=row["title"],
                            username=row["username"],
                            progress_cb=progress_cb,
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
            
            # Send completion
            yield f"data: {json.dumps({'status': 'complete'})}\n\n"
        
        return StreamingResponse(event_generator(), media_type="text/event-stream")
    
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


# ── /api/fetch ───────────────────────────────────────────────────────────
async def fetch_channels(request: Request):
    """Trigger a backfill for selected channels.
    POST body: { "channel_ids": [12, 13], "days": 7 }
    """
    try:
        body = await request.json()
        channel_ids = body.get("channel_ids", [])
        days = body.get("days", 7)
        
        if not channel_ids:
            return JSONResponse({"success": False, "message": "No channels selected"}, status_code=400)
        
        from datetime import datetime, timedelta, timezone
        from pipeline import run_backfill
        
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
            
            # Calculate window: last N days
            window_end = datetime.now(timezone.utc)
            window_start = window_end - timedelta(days=days)
            
            # Telethon ref: @username if public, else the numeric Telegram id
            channel_ref = f"@{row['username']}" if row["username"] else str(row["telegram_channel_id"])
            
            try:
                # Run backfill for this channel
                progress = run_backfill(
                    channel_ref=channel_ref,
                    window_start=window_start.replace(tzinfo=None),
                    window_end=window_end.replace(tzinfo=None),
                    title=row["title"],
                    username=row["username"],
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
        existing = conn.execute(
            "SELECT telegram_channel_id, id, title, username FROM channels"
        ).fetchall()
        existing_map = {row["telegram_channel_id"]: row for row in existing}
        
        # Build response
        out = []
        for tc in telegram_channels:
            tg_id = tc["id"]
            is_in_db = tg_id in existing_map
            
            out.append({
                "telegram_id": tg_id,
                "title": tc["title"],
                "username": tc["username"],
                "type": tc["type"],
                "in_database": is_in_db,
                "db_id": existing_map[tg_id]["id"] if is_in_db else None,
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
            
            # Check if already exists
            existing = conn.execute(
                "SELECT id FROM channels WHERE telegram_channel_id = ?",
                (telegram_id,)
            ).fetchone()
            
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


app = Starlette(routes=[
    Route("/api/channels", channels),
    Route("/api/leaderboard", leaderboard),
    Route("/api/tokens", tokens),
    Route("/api/channels/{handle}", channel_detail),
    Route("/api/channels/{handle}/buckets", channel_buckets),
    Route("/api/channels/{handle}/streak", channel_streak),
    Route("/api/channels/{handle}/calls", channel_calls),
    Route("/api/fetch-stream", fetch_stream, methods=["POST"]),
    Route("/api/fetch", fetch_channels, methods=["POST"]),
    Route("/api/telegram-channels", telegram_channels),
    Route("/api/add-channels", add_channels, methods=["POST"]),
])

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