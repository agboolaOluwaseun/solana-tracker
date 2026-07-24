"""
STANDALONE AUDIT — independent of the pipeline.

Purpose: objectively verify each detected call's real peak ROI to figure out
why the pipeline reports ~8.7% win rate when you expect >70%.

This script:
  1. Re-fetches raw messages from the channel (Telethon).
  2. Extracts Solana addresses independently (simple regex, NO bot filter, NO dedup).
  3. For EACH address, queries GeckoTerminal directly:
       - resolve pool (try /tokens/{addr}/pools, then /pools/{addr})
       - fetch 24h hourly OHLCV
       - entry = first candle open, peak = max high, ROI = (peak/entry - 1)
       - is_win = ROI >= 100% (2x)
  4. Prints a full report so we can compare against the pipeline's DB results.

Run:
    .venv/bin/python -m scripts.audit --channel cryptoyeezuscalls --start 2026-04-02
    .venv/bin/python -m scripts.audit --channel cryptoyeezuscalls --start 2026-04-02 --no-filter
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from config import settings
from ingestion.telethon_fetcher import build_client

ADDR_RE = __import__("re").compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
BASE = "https://api.geckoterminal.com/api/v2"
NET = "solana"


def gt_get(path, params=None):
    """Direct GeckoTerminal GET with retries, bypassing our RateLimiter/cache."""
    for attempt in range(5):
        try:
            r = requests.get(f"{BASE}{path}", params=params, headers={"accept": "application/json", "User-Agent": UA}, timeout=30)
            if r.status_code in (429, 403) or r.status_code >= 500:
                import time
                ra = r.headers.get("Retry-After")
                wait = float(ra) if ra else 2 ** attempt
                print(f"    [throttle {r.status_code}] waiting {wait}s")
                time.sleep(min(wait, 30))
                continue
            if r.status_code == 404:
                return None
            if r.status_code >= 400:
                print(f"    [HTTP {r.status_code}] {r.text[:120]}")
                return None
            return r.json()
        except requests.RequestException as e:
            print(f"    [retry {attempt}] {e}")
    return None


def resolve_pool(addr):
    """Returns (pool_address_bare, token_symbol) or (None, None)."""
    # Try as token first.
    data = gt_get(f"/networks/{NET}/tokens/{addr}/pools", {"page": 1})
    pools = (data or {}).get("data", []) if isinstance(data, dict) else []
    best, best_liq = None, -1
    for p in pools:
        pid = p.get("id", "")
        if not pid.startswith(f"{NET}_"):
            continue
        attrs = p.get("attributes", {})
        liq = float(attrs.get("reserve_in_usd") or 0)
        if liq > best_liq:
            best, best_liq = p, liq
    if best:
        bare = best["id"].replace(f"{NET}_", "", 1)
        sym = (best.get("attributes", {}).get("name") or "").split("/")[0].strip()
        return bare, sym
    # Try as pool.
    data = gt_get(f"/networks/{NET}/pools/{addr}")
    p = (data or {}).get("data") if isinstance(data, dict) else None
    if isinstance(p, dict):
        bare = p.get("id", "").replace(f"{NET}_", "", 1)
        sym = (p.get("attributes", {}).get("name") or "").split("/")[0].strip()
        return bare, sym
    return None, None


def fetch_ohlcv_peak(pool, call_ts):
    """Returns (entry, peak, peak_pct) or (None, None, None)."""
    end = call_ts + timedelta(hours=24)
    data = gt_get(
        f"/networks/{NET}/pools/{pool}/ohlcv/hour",
        {
            "before_timestamp": int(end.replace(tzinfo=timezone.utc).timestamp()),
            "after_timestamp": int(call_ts.replace(tzinfo=timezone.utc).timestamp()),
            "limit": 1000,
            "currency": "usd",
        },
    )
    payload = (data or {}).get("data")
    if isinstance(payload, dict):
        vals = (payload.get("attributes") or {}).get("ohlcv_list")
    elif isinstance(payload, list) and payload:
        vals = (payload[0].get("attributes") or {}).get("ohlcv_list")
    else:
        vals = None
    if not vals:
        return None, None, None
    entry = float(vals[0][1])
    peak = max(float(v[2]) for v in vals)
    return entry, peak, (peak / entry - 1) * 100 if entry else None


async def fetch_calls(channel, start, end, apply_filter):
    """Pull raw messages and extract addresses independently."""
    client = build_client()
    await client.connect()
    if not await client.is_user_authorized():
        print("NOT AUTHORIZED"); return []
    ent = await client.get_entity(channel)
    print(f"Channel: {getattr(ent,'title',channel)} | scanning {start.date()} -> {end.date()}")
    calls = []
    async for msg in client.iter_messages(ent, offset_date=end, limit=None):
        ts = msg.date
        if ts.tzinfo: ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
        if ts < start: break
        if ts >= end: continue
        text = msg.text or ""
        addrs = ADDR_RE.findall(text)
        if not addrs: continue
        # Simple bot heuristic (optional).
        if apply_filter and ("🟡" in text or "➡️ SOL" in text or "solscan.io/" in text):
            continue
        calls.append({"addr": addrs[0], "ts": ts, "msg_id": msg.id, "snippet": text[:70].replace("\n", " ")})
    await client.disconnect()
    return calls


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", required=True)
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--weeks", type=int, default=12)
    ap.add_argument("--no-filter", action="store_true", help="Disable bot-spam heuristic")
    args = ap.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d")
    end = start + timedelta(weeks=args.weeks)

    print(f"=== AUDIT: {args.channel} (filter={'off' if args.no_filter else 'on'}) ===")
    calls = asyncio.run(fetch_calls(args.channel, start, end, not args.no_filter))
    print(f"\nDetected {len(calls)} raw calls (before dedup). Deduplicating by address+24h...")
    # Dedup
    seen = {}
    deduped = []
    for c in sorted(calls, key=lambda x: x["ts"]):
        key = c["addr"]
        if key not in seen or (c["ts"] - seen[key]) >= timedelta(hours=24):
            seen[key] = c["ts"]
            deduped.append(c)
    print(f"After dedup: {len(deduped)} unique calls.\n")

    wins, losses, unpriceable = 0, 0, 0
    for i, c in enumerate(deduped, 1):
        addr_short = c["addr"][:10] + "..." + c["addr"][-4:]
        print(f"[{i}/{len(deduped)}] {c['ts'].date()} {addr_short}  \"{c['snippet']}\"")
        pool, sym = resolve_pool(c["addr"])
        if not pool:
            print(f"        -> NO POOL (unpriceable)")
            unpriceable += 1
            continue
        entry, peak, pct = fetch_ohlcv_peak(pool, c["ts"])
        if entry is None:
            print(f"        -> {sym or '?'} NO OHLCV (unpriceable)")
            unpriceable += 1
            continue
        is_win = pct >= 100
        status = "✅ WIN" if is_win else "❌ LOSS"
        wins += 1 if is_win else 0
        losses += 0 if is_win else 1
        print(f"        -> {sym or '?'} entry=${entry:.8f} peak=${peak:.8f} ROI=+{pct:.0f}% {status}")

    total = wins + losses + unpriceable
    priceable = wins + losses
    print(f"\n{'='*60}")
    print(f"TOTAL: {total} calls | {wins} wins | {losses} losses | {unpriceable} unpriceable")
    if priceable:
        print(f"Win rate (priceable only): {wins/priceable*100:.1f}%")
    if total:
        print(f"Win rate (all calls, unpriceable=loss): {wins/total*100:.1f}%")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
