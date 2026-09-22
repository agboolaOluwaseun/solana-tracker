"""
Cross-chain Birdeye rescue for unpriceable calls.

Proven 2026-09 by a one-off DB-wide sweep: 195 of 255 'unpriceable_loss'
calls across 19 channels had COMPLETE price history on Birdeye — they were
'dead' only because DexScreener/GeckoTerminal never indexed them OR our
chain tag was wrong (a bare 0x address defaults to a guessed chain, and
'no pool' on the wrong chain looked like a delisted token). Most
surprisingly, ~40% of 'robinhood'-tagged no-pools were live ethereum/base/
bsc tokens, and Birdeye carries deeper Solana history than GT free tier.

This module turns that into a permanent second opinion: whenever the 7d
engine ends with `unpriceable_loss`, we probe Birdeye ADDRESS-FIRST (no
chain assumption — the whole point) across every EVM chain it indexes
(base58 -> solana only), fetch the call's full 7d hourly window on the
chain that answers, and run it through the SAME evaluate_call_7d engine
(1m candles are fetched lazily by the engine's screening gate, so dead
tokens cost one probe + one 1H call). A successful rescue overwrites the
verdict AND corrects the stored chain tag so every future pass looks on
the right chain without Birdeye.

Discipline:
  * opt-out flag: BIRDEYE_RESCUE=false (also off automatically when no
    BIRDEYE_API_KEY is set — rescue is a bonus, never a hard dependency);
  * one shared pacing clock (free tier ~10 req/min): probes 1.6s apart,
    OHLCV 7.6s — generous on purpose, this runs inside the normal pass;
  * circuit breaker: 6 consecutive net/429 failures disable the rescue
    for the remainder of the process (rescore loops must never stall
    because a third-party API is having a bad day);
  * every exception is swallowed -> the caller keeps its original verdict.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from config import settings
from models import PricePoint
from pricing.strategy7d import Eval7dResult, evaluate_call_7d

log = logging.getLogger(__name__)

# Birdeye chain slugs we probe (order = cheap-first when the stored tag is
# already EVM: probe the tagged chain first, then the others).
EVM_CHAINS = ("bsc", "base", "ethereum", "robinhood")
TO_INTERNAL = {"bsc": "bsc", "base": "base", "ethereum": "eth",
               "robinhood": "robinhood", "solana": "sol"}

_lock = threading.Lock()
_last = {"price": 0.0, "ohlcv": 0.0}
_consecutive_failures = 0
_MAX_FAILURES = 6
_chain_cache: dict[str, Optional[str]] = {}

_BASE = "https://public-api.birdeye.so"
_session = requests.Session()


def enabled() -> bool:
    return bool(settings.birdeye_api_key) and settings.birdeye_rescue and _consecutive_failures < _MAX_FAILURES


def reset_breaker() -> None:
    """Re-arm the rescue (e.g. at the start of a new pass/session)."""
    global _consecutive_failures
    with _lock:
        _consecutive_failures = 0


def _pace(kind: str, interval: float) -> None:
    with _lock:
        gap = interval - (time.time() - _last[kind])
        if gap > 0:
            time.sleep(gap)
        _last[kind] = time.time()


def _fail() -> None:
    global _consecutive_failures
    with _lock:
        _consecutive_failures += 1
        if _consecutive_failures == _MAX_FAILURES:
            log.warning("birdeye rescue: %d consecutive failures — disabled for this run",
                        _consecutive_failures)


def _ok() -> None:
    global _consecutive_failures
    with _lock:
        _consecutive_failures = 0


def _get(path: str, chain: str, params: dict, kind: str, tries: int = 3):
    headers = {"accept": "application/json", "x-chain": chain}
    headers.update({"X-API-KEY": settings.birdeye_api_key})
    for i in range(tries):
        _pace(kind, 1.6 if kind == "price" else 7.6)
        try:
            r = _session.get(_BASE + path, params=params, headers=headers, timeout=25)
            if r.status_code == 429:
                time.sleep(8 + 4 * i)
                continue
            if r.status_code == 200:
                _ok()
                return r
            return None
        except requests.RequestException:
            continue
    _fail()
    return None


def _price_probe(addr: str, chain: str) -> Optional[bool]:
    """True = token has a live price on this chain, False = the chain
    answered "no token here", None = the probe itself failed (network/429).
    None must NEVER be cached as False — a flaky chain would hide a token
    that lives there."""
    r = _get("/defi/price", chain, {"address": addr}, "price")
    if r is None:
        return None
    try:
        return (r.json().get("data") or {}).get("value") is not None
    except Exception:
        return None


def find_chain(addr: str, our_chain: str) -> Optional[str]:
    """Chain-agnostic: which Birdeye chain has a live price for this mint?

    base58 (Solana) addresses are only ever probed on 'solana'; an 0x
    address is probed on its tagged chain FIRST (cheap when the tag is
    right) then the other EVM chains. Definitive answers cached per
    process; probe failures are not cached (retry next call)."""
    if addr in _chain_cache:
        return _chain_cache[addr]
    found: Optional[str] = None
    complete = True  # False if any probe failed -> don't cache a miss
    if not addr.startswith("0x"):
        hit = _price_probe(addr, "solana")
        found = "solana" if hit else None
        complete = hit is not None
    else:
        internal = our_chain
        external = {"bsc": "bsc", "base": "base", "eth": "ethereum",
                    "ethereum": "ethereum", "robinhood": "robinhood"}.get(internal)
        order = [c for c in EVM_CHAINS if c == external] \
              + [c for c in EVM_CHAINS if c != external]
        for ch in order:
            hit = _price_probe(addr, ch)
            if hit is None:
                complete = False
                continue
            if hit:
                found = ch
                break
    if complete or found:
        _chain_cache[addr] = found
    return found


def _ohlcv_points(addr: str, chain: str, t_from: int, t_to: int, typ: str):
    r = _get("/defi/ohlcv", chain,
             {"address": addr, "type": typ, "time_from": t_from, "time_to": t_to},
             "ohlcv")
    if r is None:
        return []
    try:
        items = (r.json().get("data") or {}).get("items") or []
    except Exception:
        return []
    pts = []
    for it in items:
        try:
            ts = datetime.fromtimestamp(int(it["unixTime"]), tz=timezone.utc).replace(tzinfo=None)
            pts.append(PricePoint(
                timestamp=ts, open=float(it["o"]), high=float(it["h"]),
                low=float(it["l"]), close=float(it["c"]),
                volume=float(it.get("vol", 0) or it.get("v", 0) or 0)))
        except Exception:
            continue
    pts.sort(key=lambda p: p.timestamp)
    return pts


def _identity(addr: str, ext_chain: str):
    """Best-effort symbol/name + pool via DexScreener on the FOUND chain.
    Failure is cosmetic — the verdict doesn't depend on it."""
    try:
        from pipeline import _get_ds_client
        pi = _get_ds_client().resolve_pool(addr, chain=ext_chain)
        if pi:
            return pi.get("pool_address"), pi.get("symbol"), pi.get("name")
    except Exception:
        pass
    return None, None, None


def try_rescue(token_address: str, chain: str, call_ts: datetime,
               now: Optional[datetime] = None) -> Optional[Eval7dResult]:
    """Return a scored Eval7dResult (status win/loss, `chain_corrected` set)
    or None to leave the caller's unpriceable verdict in place. NEVER
    raises — a broken third-party API must not change pipeline behavior."""
    try:
        if not enabled():
            return None
        ts = call_ts
        if ts.tzinfo is not None:
            ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
        bc = find_chain(token_address, chain)
        if not bc:
            log.info("birdeye rescue: %s known on no chain — leaving unpriceable",
                     token_address[:12] + "…")
            return None
        t0 = int((ts - timedelta(hours=1)).replace(tzinfo=timezone.utc).timestamp()) // 3600 * 3600
        t1 = int((ts + timedelta(days=8)).replace(tzinfo=timezone.utc).timestamp())
        hourly = _ohlcv_points(token_address, bc, t0, t1, "1H")
        if not hourly:
            log.info("birdeye rescue: %s live on %s but zero candles — leaving",
                     token_address[:12] + "…", bc)
            return None

        minute_cache: dict[tuple, list] = {}

        def fetch_hourly(pool, token, start, end):
            return [p for p in hourly if start <= p.timestamp <= end], 0

        def fetch_minute(pool, token, start, end):
            key = (start.isoformat(), end.isoformat())
            if key not in minute_cache:
                s = int(start.replace(tzinfo=timezone.utc).timestamp()) // 60 * 60
                e = int(end.replace(tzinfo=timezone.utc).timestamp()) // 60 * 60
                minute_cache[key] = _ohlcv_points(token_address, bc, s, e, "1m")
            return [p for p in minute_cache[key] if start <= p.timestamp <= end], 1

        pool_addr, sym, nm = _identity(token_address, "ethereum" if bc == "ethereum" else bc)
        r = evaluate_call_7d(pool_addr or token_address, token_address, ts,
                             fetch_hourly, fetch_minute,
                             eval_days=settings.eval_days,
                             entry_grace_minutes=settings.entry_grace_minutes,
                             now=now)
        if r.status_plain == "unpriceable_loss":
            return None
        r.token_symbol = sym or None
        r.token_name = nm or None
        r.chain_corrected = TO_INTERNAL.get(bc, bc)
        r.note = f"{r.note} (birdeye {bc})" if r.note else f"birdeye {bc}"
        log.info("birdeye rescue: %s %s -> %s (chain %s->%s)",
                 (sym or token_address[:10]), ts.isoformat(timespec="minutes"),
                 r.status_plain, chain, r.chain_corrected)
        return r
    except Exception:  # noqa: BLE001 — rescue is optional by design
        log.exception("birdeye rescue failed for %s — keeping original verdict",
                      token_address[:12] + "…")
        return None
