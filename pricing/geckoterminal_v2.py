"""
geckoterminal_v2.py — Optimized GeckoTerminal client.

Changes from the original (geckoterminal.py):
  1. Removes the double jitter (the 0.4-1.8s sleep in _request is removed;
     the limiter's built-in jitter is sufficient).
  2. Uses RateLimiterV2 (safety_margin=0.85, shorter cooldowns, adaptive boost).
  3. Lets the circuit breaker gate retries (tenacity no longer adds its own
     exponential backoff on top of the breaker's cooldown).

Everything else is identical — same endpoints, same parsing, same cache logic.
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import settings
from models import PricePoint
from pricing import cache as cache_mod
from pricing.rate_limiter_v2 import RateLimiterV2

log = logging.getLogger(__name__)

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class GeckoTerminalError(Exception):
    pass


class _RetryableAPIError(GeckoTerminalError):
    """429 / 403 / 5xx — retryable."""


class GeckoTerminalClientV2:
    def __init__(self, requests_per_minute: Optional[int] = None, network: Optional[str] = None):
        self.base_url = settings.geckoterminal_base_url
        # network: 'solana' (default) | 'robinhood' — selects the GT network for
        # every endpoint this client hits (chain-agnostic 7d engine support).
        self.network = network or settings.solana_network
        self.session = requests.Session()
        self.session.headers.update({"accept": "application/json", "User-Agent": _BROWSER_UA})
        if settings.geckoterminal_api_key:
            self.session.headers["x-cg-pro-api-key"] = settings.geckoterminal_api_key
        # SINGLE SHARED limiter across ALL endpoints. GeckoTerminal's rate limit
        # is global per IP and behaves as a TOKEN BUCKET (~6 request capacity,
        # refilling ~5/min) — empirically measured:
        #   - Burst of 15 at 1/s: 6 OK then 429s  (bucket capacity ~6)
        #   - Sustained 8/min: 6/24 throttled      (refill < 8/min)
        #   - Sustained 5/min: 16/16 clean          (refill ~5/min)
        # Pacing AT the refill rate (5/min) is the fastest clean sustained pace;
        # anything faster accumulates 429 cooldowns that cost more net time.
        rpm = requests_per_minute or 5
        self.limiter = RateLimiterV2(rpm, safety_margin=1.0, min_rpm=2)
        self.ohlcv_limiter = self.limiter  # compatibility aliases
        self.search_limiter = self.limiter
        # Request counters for instrumentation
        self.request_count = 0
        self.throttle_count = 0

    # ---- internals ---------------------------------------------------------

    def _request(self, path: str, params: Optional[dict] = None) -> dict:
        url = f"{self.base_url}{path}"
        limiter = self.limiter
        
        # tenacity is now the outer safety net only — min=2s (was 4s), max=30s (was 120s).
        # The circuit breaker in the limiter handles the real backoff for 429s.
        retryer = Retrying(
            reraise=True,
            retry=retry_if_exception_type((_RetryableAPIError, requests.RequestException)),
            wait=wait_exponential(multiplier=1, min=2, max=30),
            stop=stop_after_attempt(5),
        )
        for attempt in retryer:
            with attempt:
                limiter.acquire()
                self.request_count += 1
                log.debug("GET %s params=%s", path, params)
                resp = self.session.get(url, params=params, timeout=30)

                if resp.status_code in (429, 403) or resp.status_code >= 500:
                    if resp.status_code == 429:
                        self.throttle_count += 1
                        retry_after = _parse_retry_after(resp)
                        limiter.notify_throttled(retry_after)
                        log.info(
                            "HTTP 429 on %s — cooldown engaged (target now %s RPM)",
                            path, limiter.target_rpm,
                        )
                        # Short sleep — the limiter's cooldown gates the next attempt.
                        time.sleep(min(max(retry_after or 0, 3.0), 15.0))
                    elif resp.status_code == 403:
                        self.throttle_count += 1
                        retry_after = _parse_retry_after(resp)
                        limiter.notify_throttled(retry_after)
                        log.warning("HTTP 403 (Cloudflare) on %s — backing off", path)
                        time.sleep(min(max(retry_after or 0, 10.0), 30.0))
                    else:
                        time.sleep(1.0)
                    raise _RetryableAPIError(
                        f"HTTP {resp.status_code} from {path}: {resp.text[:200]}"
                    )
                if resp.status_code == 401:
                    # 401 is a client error (unauthorized/payment required), not retryable
                    raise GeckoTerminalError(
                        f"HTTP 401 from {path}: {resp.text[:200]}"
                    )
                if resp.status_code == 404:
                    raise GeckoTerminalError(f"Not found (404): {path}")
                if resp.status_code >= 400:
                    raise GeckoTerminalError(
                        f"HTTP {resp.status_code} from {path}: {resp.text[:200]}"
                    )
                limiter.notify_success()
                return resp.json()
        raise GeckoTerminalError("retry loop exited without a value")

    # ---- pool resolution (identical to original) --------------------------

    def _pick_best_solana_pool(self, pools: list, requested_addr: str) -> Optional[dict]:
        best = None
        best_liq = -1.0
        for p in pools:
            pool_id = p.get("id") or (p.get("attributes") or {}).get("address")
            if not pool_id:
                continue
            if not str(pool_id).startswith(f"{self.network}_"):
                continue
            attrs = p.get("attributes", {})
            liq = attrs.get("reserve_in_usd") or (attrs.get("volume_usd") or {}).get("h24") or 0
            try:
                liq_f = float(liq)
            except (TypeError, ValueError):
                liq_f = 0.0
            if liq_f > best_liq:
                best_liq = liq_f
                best = p

        if best is None:
            return None
        attrs = best.get("attributes", {})
        bare_pool = _strip_network_prefix(best.get("id"), self.network)
        base_token_addr = _extract_base_token_address(best, self.network) or requested_addr

        return {
            "pool_address": bare_pool,
            "dex_pool_id": best.get("id"),
            "token_address": base_token_addr,
            "liquidity_usd": best_liq,
            "symbol": _extract_token_symbol(best) or attrs.get("name"),
            "name": attrs.get("name"),
        }

    def resolve_pool_smart(self, address: str) -> Optional[dict]:
        # 1. PRIMARY: /search/pools
        try:
            data = self._request("/search/pools", params={"query": address, "network": self.network})
        except GeckoTerminalError as e:
            log.warning("search/pools failed for %s: %s", address, e)
            data = {"data": []}

        result = self._pick_best_solana_pool((data or {}).get("data", []) or [], address)
        if result:
            return result

        # 2. FALLBACK A: token endpoint
        try:
            data = self._request(f"/networks/{self.network}/tokens/{address}/pools", params={"page": 1})
            result = self._pick_best_solana_pool((data or {}).get("data", []) or [], address)
            if result:
                return result
        except GeckoTerminalError:
            pass

        # 3. FALLBACK B: pool endpoint
        try:
            data = self._request(f"/networks/{self.network}/pools/{address}")
            result = self._pick_best_solana_pool((data or {}).get("data", []) or [], address)
            if result:
                return result
        except GeckoTerminalError:
            pass

        return None

    # ---- OHLCV (identical to original) ------------------------------------

    def fetch_ohlcv(
        self,
        pool_address: str,
        token_address: str,
        start: datetime,
        end: datetime,
        aggregate: str = "hour",
    ) -> List[PricePoint]:
        pool_address = _strip_network_prefix(pool_address, self.network)
        _all_aggs = ["minute", "hour", "1", "day"]
        aggregate_candidates = [aggregate] + [a for a in _all_aggs if a != aggregate]

        agg_minutes_for_cache = {"minute": 1, "hour": 60, "day": 1440}.get(aggregate, 60)
        cached = cache_mod.load_candles(pool_address, token_address, start, end)
        expected = max(1, int((end - start).total_seconds() // 60 // agg_minutes_for_cache))

        if cached:
            cached_start = cached[0].timestamp
            cached_end = cached[-1].timestamp + timedelta(minutes=agg_minutes_for_cache)
            leading_gap = cached_start > start + timedelta(minutes=agg_minutes_for_cache)
            trailing_gap = cached_end < end - timedelta(minutes=agg_minutes_for_cache)
        else:
            leading_gap = True
            trailing_gap = True

        if not leading_gap and not trailing_gap and len(cached) >= expected:
            return _fill_and_sort(cached, start, end)

        fetched: List[PricePoint] = []
        max_candle = 1000
        agg_minutes = {"minute": 1, "hour": 60, "day": 1440}.get(aggregate, 60)
        chunk_duration = timedelta(minutes=900 * agg_minutes)
        safety = 0
        last_err = None

        def _fetch_span(span_start, span_end):
            nonlocal safety, last_err
            span_fetched: List[PricePoint] = []
            chunk_start = span_start
            while chunk_start < span_end and safety < 40:
                safety += 1
                chunk_end = min(span_end, chunk_start + chunk_duration)
                params = {
                    "before_timestamp": int(chunk_end.replace(tzinfo=timezone.utc).timestamp()),
                    "after_timestamp": int(chunk_start.replace(tzinfo=timezone.utc).timestamp()),
                    "limit": max_candle,
                    "currency": "usd",
                }
                data = None
                for agg in aggregate_candidates:
                    path = f"/networks/{self.network}/pools/{pool_address}/ohlcv/{agg}"
                    try:
                        data = self._request(path, params=params)
                        break
                    except GeckoTerminalError as e:
                        last_err = e
                        msg = str(e)
                        if "Invalid" in msg and ("aggregate" in msg or "timeframe" in msg):
                            continue
                        else:
                            break
                if data is None:
                    log.warning("ohlcv fetch failed for pool %s: %s", pool_address, last_err)
                    break
                candles = _parse_ohlcv(data)
                if not candles:
                    break
                span_fetched.extend(candles)
                # Two complete-response signals (either one means no pagination):
                # 1. Fewer candles than the limit -> the API returned EVERYTHING
                #    available; nothing left to fetch.
                # 2. The response extends back to/BEFORE the window start.
                #    GeckoTerminal ignores after_timestamp and returns the latest
                #    `limit` candles before before_timestamp, so a response that
                #    already covers span_start is complete for our window even
                #    when it hits the 1000-candle cap (dead tokens return
                #    ~36h of history in one shot).
                earliest_ts = min(c.timestamp for c in candles)
                if len(candles) < max_candle or earliest_ts <= span_start:
                    break
                latest = max(c.timestamp for c in candles)
                if latest <= chunk_start:
                    break
                chunk_start = latest + timedelta(minutes=agg_minutes)
            return span_fetched

        if leading_gap:
            if cached:
                fetched.extend(_fetch_span(start, min(cached[0].timestamp, end)))
            else:
                fetched.extend(_fetch_span(start, end))

        if trailing_gap:
            if cached:
                gap_start = cached[-1].timestamp + timedelta(minutes=agg_minutes)
                if gap_start < end:
                    fetched.extend(_fetch_span(gap_start, end))

        all_candles = list(cached) + fetched
        seen_ts = set()
        merged = []
        for c in sorted(all_candles, key=lambda x: x.timestamp):
            ts_key = c.timestamp.replace(microsecond=0)
            if ts_key not in seen_ts:
                seen_ts.add(ts_key)
                merged.append(c)

        if fetched:
            cache_mod.store_candles(pool_address, token_address, fetched)

        return _fill_and_sort(merged, start, end)


# ---- helpers (identical to original) --------------------------------------

def _strip_network_prefix(pool_id: Optional[str], network: str) -> str:
    if not pool_id:
        return ""
    prefix = f"{network}_"
    if pool_id.startswith(prefix):
        return pool_id[len(prefix):]
    return pool_id


def _parse_retry_after(resp) -> Optional[float]:
    val = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
    if not val:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _extract_base_token_address(pool_obj: dict, network: str) -> Optional[str]:
    rel = pool_obj.get("relationships", {}) or {}
    bt = (rel.get("base_token") or {}).get("data") or {}
    bt_id = bt.get("id")
    if bt_id:
        return _strip_network_prefix(bt_id, network)
    attrs = pool_obj.get("attributes", {}) or {}
    return attrs.get("base_token", {}).get("address")


def _extract_token_symbol(pool_obj: dict) -> Optional[str]:
    attrs = pool_obj.get("attributes", {}) or {}
    name = attrs.get("name") or ""
    if name and "/" in name:
        return name.split("/")[0].strip()
    bt = (attrs.get("base_token") or {})
    return bt.get("symbol")


def _parse_ohlcv(data: dict) -> List[PricePoint]:
    out: List[PricePoint] = []
    payload = (data or {}).get("data")
    if payload is None:
        items = []
    elif isinstance(payload, dict):
        items = [payload]
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    for item in items:
        if not isinstance(item, dict):
            continue
        vals = (item.get("attributes") or {}).get("ohlcv_list")
        if not vals:
            continue
        for row in vals:
            try:
                ts_s, o, h, l, c, v = row[0], row[1], row[2], row[3], row[4], row[5]
                out.append(
                    PricePoint(
                        timestamp=datetime.fromtimestamp(int(ts_s), tz=timezone.utc).replace(tzinfo=None),
                        open=float(o), high=float(h), low=float(l),
                        close=float(c), volume=float(v) if v is not None else 0.0,
                    )
                )
            except (TypeError, ValueError, IndexError):
                continue
    return out


def _fill_and_sort(candles: List[PricePoint], start: datetime, end: datetime) -> List[PricePoint]:
    if not candles:
        return []
    candles = [c for c in candles if start <= c.timestamp < end]
    candles.sort(key=lambda c: c.timestamp)
    return candles
