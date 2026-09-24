"""
/api/ai-chat — KOLfi's data-grounded AI assistant (Qwen via the same
OpenAI-compatible endpoint that powers the Hermes chat).

Design rules:
  * This module is imported lazily by server.py inside try/except: nothing
    here may run at import time that can fail (no network, no key reads).
  * Every failure path returns a clean JSON error envelope so the chat page
    can show a banner — the rest of the app never touches this code.
  * The model is grounded with a compact numeric snapshot of the DB
    (per-channel decided stats for several windows/chains, computed by the
    same analysis functions the UI reads), so answers match the app's own
    metrics exactly — including the rule that unpriceable/pending never
    enter a win rate.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Optional

import requests

log = logging.getLogger(__name__)

# Same endpoint Hermes uses for this chat (Alibaba Token Plan, OpenAI-
# compatible). Overridable via env for a different subscription.
_DEFAULT_BASE_URL = "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
_DEFAULT_MODEL = "qwen3.8-flash"
_KEY_SOURCES = ("AI_CHAT_API_KEY", "DASHSCOPE_API_KEY", "ALIBABA_TOKEN_PLAN_API_KEY")

_lock = threading.Lock()
_key_cache: Optional[str] = None
_key_cache_at = 0.0


def _read_hermes_env_key() -> Optional[str]:
    """Last resort: the Hermes CLI keeps its provider keys in ~/.hermes/.env.
    Read only the KEY name we need; never log values."""
    path = os.path.expanduser("~/.hermes/.env")
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                m = re.match(r"^DASHSCOPE_API_KEY=(\S{8,})", line.strip())
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def _api_key() -> Optional[str]:
    """Resolve the AI key: project env (.env via config.load_dotenv) first,
    then the Hermes CLI env file. Cached ~60s so a renewed/replaced key is
    picked up without a server restart."""
    global _key_cache, _key_cache_at
    import time as _t
    now = _t.monotonic()
    with _lock:
        if _key_cache and now - _key_cache_at < 60:
            return _key_cache
        key = None
        for var in _KEY_SOURCES:
            val = os.environ.get(var)
            if val:
                key = val
                break
        if not key:
            key = _read_hermes_env_key()
        if key:
            key = key.strip().strip("'\"")
            # A key pasted from a document can carry smart quotes/em dashes;
            # HTTP headers are latin-1 only and requests would raise deep in
            # the call. Treat it as unconfigured with a clear message instead.
            if not key.isascii():
                log.warning("AI key contains non-ASCII characters — ignoring it")
                key = None
        _key_cache = key
        _key_cache_at = now
        return key


# ── data snapshot ────────────────────────────────────────────────────────────

def _build_snapshot(conn) -> dict:
    """Compact, UI-consistent stats for every channel × window × chain.
    Numbers come from the SAME analysis helpers the app renders, so the
    assistant cannot disagree with the dashboard."""
    from analysis import windowed as W

    channels = conn.execute(
        "SELECT id, username, title FROM channels ORDER BY title"
    ).fetchall()
    rows = []
    for ch in channels:
        entry = {"title": ch["title"], "username": ch["username"], "windows": {}}
        for window in ("7d", "1m", "3m", "all"):
            st = W.channel_stats_window(ch["id"], window, "normal", chain=None)
            if not st["total_calls"]:
                continue
            w = {
                "decided": st["total_calls"],
                "wins": st["wins"],
                "win_rate_pct": round(st["win_rate"], 1) if st["win_rate"] is not None else None,
                "avg_peak_profit_pct": round(st["avg_peak_profit_pct"], 1)
                    if st["avg_peak_profit_pct"] is not None else None,
                "streak": W.current_streak(ch["id"], "normal", chain=None),
            }
            # per-chain split for the all-chains population
            chains = conn.execute(
                "SELECT chain, COUNT(*) n FROM calls WHERE channel_id=?"
                " AND status IN ('win','loss') GROUP BY chain",
                (ch["id"],),
            ).fetchall()
            w["by_chain"] = {r["chain"]: r["n"] for r in chains}
            entry["windows"][window] = w
        # whole-history context: totals incl. unpriceable, so the model can
        # explain coverage honestly when asked "why so many unpriceable"
        extra = conn.execute(
            "SELECT COUNT(*) total,"
            " SUM(status='unpriceable_loss') unp,"
            " SUM(status='pending') pend FROM calls WHERE channel_id=?",
            (ch["id"],),
        ).fetchone()
        entry["totals"] = {"rows": extra["total"], "unpriceable": extra["unp"] or 0,
                           "pending": extra["pend"] or 0}
        rows.append(entry)
    return {"generated_utc": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).isoformat(timespec="seconds"),
            "channels": rows}


_SYSTEM_PROMPT = """You are the KOLfi tracker's assistant. KOLfi scores Telegram crypto-call channels: a call is a WIN if the token reached 2x within 7 days of the call timestamp, a LOSS if it dropped 50% first; same-candle 2x/-50% counts as a WIN (user rule). Win rate = wins/(wins+losses) over DECIDED calls only — unpriceable (dead/delisted token, or a token living on a chain outside Solana/Robinhood/Ethereum/BSC/Base/Arc) and pending calls are excluded from the denominator, never counted as losses. 'live' = provisional verdict on a call younger than 7 days, refreshed on every app launch/Refresh All. Chains tracked: sol, robinhood, eth, bsc, base, arc.

Answer ONLY database questions using the DATA SNAPSHOT below (numbers identical to what the app displays). STRATEGY TABLES: stoploss_results = 50% fixed stop, trailing_results = 50% trailing stop; both have status 'win'/'loss' (a trigger fired), 'expired' (window closed with neither 2x nor the stop hitting — UNDECIDED, never in any win-rate math), 'pending', 'unpriceable_loss'. When asked about a stop strategy's win rate or per-channel stats, join its own table and divide by status IN ('win','loss') only — expired/pending/unpriceable rows are invisible to the rate. The 'calls' count shown per strategy on cards INCLUDES expired tiles. If a question needs data not in the snapshot (a specific call's raw message, per-token entries, monthly buckets), say so briefly and suggest where in the app to look (Channels grid, deep-dive, Tokens page) instead of guessing. Be concise, plain, no flattery, no em-dash-heavy AI prose. Currency: multipliers are x, profits are peak percentage.

DATA SNAPSHOT (UTC):
"""


def _err(status: int, kind: str, message: str):
    from starlette.responses import JSONResponse
    return JSONResponse({"success": False, "error": kind, "message": message},
                        status_code=status)


async def ai_chat(request):
    """POST {messages:[{role:'user'|'assistant',content:str}]} ->
    {success:true, reply:str}. Clean JSON errors (never 500 crashes):
    not_configured / auth / rate_limited / upstream / timeout / bad_request."""
    try:
        body = await request.json()
    except Exception:
        return _err(400, "bad_request", "Expected JSON body.")
    messages = body.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return _err(400, "bad_request", "messages[] is empty.")
    # keep the last 12 turns; cap message size defensively
    messages = messages[-12:]
    if any(not isinstance(m, dict) or "content" not in m or m.get("role") not in ("user", "assistant")
           for m in messages):
        return _err(400, "bad_request", "Each message needs role user|assistant and content.")
    if sum(len(str(m["content"])) for m in messages) > 60_000:
        return _err(400, "bad_request", "Conversation too long — start a new chat.")

    key = _api_key()
    if not key:
        return _err(200, "not_configured",
                    "AI chat is not configured: add AI_CHAT_API_KEY (or DASHSCOPE_API_KEY) "
                    "to .env, or renew the subscription behind it. Everything else in KOLfi "
                    "works without it.")

    base_url = os.environ.get("AI_CHAT_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")
    model = os.environ.get("AI_CHAT_MODEL", _DEFAULT_MODEL)

    try:
        from db import get_connection
        snapshot = _build_snapshot(get_connection())
    except Exception:
        log.exception("snapshot build failed")
        snapshot = {"error": "snapshot unavailable"}

    system = _SYSTEM_PROMPT + json.dumps(snapshot, ensure_ascii=False)

    import asyncio
    def _call():
        return requests.post(
            f"{base_url}/chat/completions",
            headers={"authorization": f"Bearer {key}", "content-type": "application/json"},
            json={"model": model, "temperature": 0.3,
                  "messages": [{"role": "system", "content": system}] + messages},
            timeout=60,
        )

    try:
        resp = await asyncio.get_event_loop().run_in_executor(None, _call)
    except requests.Timeout:
        return _err(200, "timeout", "The AI provider took too long (60s). Try again.")
    except requests.RequestException as e:
        return _err(200, "upstream", f"Can't reach the AI provider: {type(e).__name__}. "
                                     "Check the network or AI_CHAT_BASE_URL.")
    except Exception:
        # Belt & braces: NOTHING in this module may surface as a 500 — the
        # rest of the app must keep working no matter how broken the AI sub is.
        log.exception("ai-chat unexpected failure")
        return _err(200, "internal", "The assistant hit an unexpected error. "
                                     "Try again; other app features are unaffected.")

    if resp.status_code in (401, 403):
        return _err(200, "auth",
                    "The AI provider rejected the key (expired subscription or quota?). "
                    "The rest of the app is unaffected.")
    if resp.status_code == 429:
        return _err(200, "rate_limited", "AI rate limit hit — wait a minute and retry.")
    if resp.status_code != 200:
        return _err(200, "upstream",
                    f"AI provider returned HTTP {resp.status_code}. Try again later.")
    try:
        data = resp.json()
        reply = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError):
        return _err(200, "upstream", "Unexpected response shape from the AI provider.")
    from starlette.responses import JSONResponse
    return JSONResponse({"success": True, "reply": reply, "model": model})
