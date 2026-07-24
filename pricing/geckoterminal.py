"""
GeckoTerminal API client.

Operations backing the backfill:
  1. resolve_pool_smart(address) -> the highest-liquidity Solana pool for an
     address that may be EITHER a token mint OR a pool address (auto-detected
     via /search/pools), with token-endpoint fallbacks.
  2. fetch_ohlcv(pool_address, token_address, start, end) -> hourly candles.

Cloudflare / rate-limit resilience (correctness over speed):
  - Realistic browser User-Agent header.
  - Sliding-window throttle (RateLimiter) + random jitter so requests don't
    arrive at perfectly regular intervals.
  - Retry-After honored when present on 429/403/503.
  - tenacity exponential backoff as the outer retry loop.

IMPORTANT prefix handling: GeckoTerminal pool ids are network-prefixed
(e.g. "solana_HNGjLLZk..."). We ALWAYS strip the "solana_" prefix before
using an id in an OHLCV path, otherwise the URL doubles the network segment
("/networks/solana/pools/solana_.../ohlcv") and returns 404.
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
from pricing.rate_limiter import RateLimiter

log = logging.getLogger(__name__)

# Realistic browser UA so Cloudflare doesn't flag us as an obvious bot.
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class GeckoTerminalError(Exception):
    pass


class _RetryableAPIError(GeckoTerminalError):
    """429 / 403 / 5xx — retryable (Cloudflare or server transient)."""


class GeckoTerminalClient:
    def __init__(self, requests_per_minute: Optional[int] = None):
        self.base_url = settings.geckoterminal_base_url
        self.network = settings.solana_network
        self.session = requests.Session()
        self.session.headers.update({"accept": "application/json", "User-Agent": _BROWSER_UA})
        if settings.geckoterminal_api_key:
            self.session.headers["x-cg-pro-api-key"] = settings.geckoterminal_api_key
        rpm = requests_per_minute or settings.requests_per_minute
        self.limiter = RateLimiter(rpm)

    # ---- internals ---------------------------------------------------------

    def _request(self, path: str, params: Optional[dict] = None) -> dict:
        url = f"{self.base_url}{path}"
        retryer = Retrying(
            reraise=True,
            retry=retry_if_exception_type((_RetryableAPIError, requests.RequestException)),
            wait=wait_exponential(multiplier=2, min=4, max=120),
            stop=stop_after_attempt(7),
        )
        for attempt in retryer:
            with attempt:
                self.limiter.acquire()
                # Random jitter between requests to look less robotic to Cloudflare.
                time.sleep(random.uniform(0.4, 1.8))
                log.debug("GET %s params=%s", path, params)
                resp = self.session.get(url, params=params, timeout=30)

                # Retryable Cloudflare / server conditions.
                if resp.status_code in (429, 403) or resp.status_code >= 500:
                    retry_after = _parse_retry_after(resp)
                    if retry_after:
                        log.info(
                            "HTTP %s on %s — honoring Retry-After %ss",
                            resp.status_code, path, retry_after,
                        )
                        time.sleep(min(retry_after, 60))
                    raise _RetryableAPIError(
                        f"HTTP {resp.status_code} from {path}: {resp.text[:200]}"
                    )
                if resp.status_code == 404:
                    raise GeckoTerminalError(f"Not found (404): {path}")
                if resp.status_code >= 400:
                    raise GeckoTerminalError(
                        f"HTTP {resp.status_code} from {path}: {resp.text[:200]}"
                    )
                return resp.json()
        raise GeckoTerminalError("retry loop exited without a value")

    # ---- pool resolution ---------------------------------------------------

    def _pick_best_solana_pool(self, pools: list, requested_addr: str) -> Optional[dict]:
        """
        Given a list of pool objects from various GeckoTerminal endpoints, pick
        the highest-liquidity Solana pool and return a normalized dict with a
        BARE pool address (no 'solana_' prefix).
        """
        best = None
        best_liq = -1.0
        for p in pools:
            pool_id = p.get("id") or (p.get("attributes") or {}).get("address")
            if not pool_id:
                continue
            # Filter to our network. Pool ids are prefixed "solana_...".
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

        # Resolve the base token address from relationships (preferred) or attrs.
        base_token_addr = _extract_base_token_address(best, self.network) or requested_addr

        return {
            "pool_address": bare_pool,
            "dex_pool_id": best.get("id"),   # keep the prefixed id as the canonical ref
            "token_address": base_token_addr,
            "liquidity_usd": best_liq,
            "symbol": _extract_token_symbol(best) or attrs.get("name"),
            "name": attrs.get("name"),
        }

    def resolve_pool_smart(self, address: str) -> Optional[dict]:
        """
        Resolve an address (token mint OR pool) to its highest-liquidity Solana
        pool. Auto-detects type via /search/pools, then falls back to explicit
        token/pool endpoints if search is empty/fuzzy.

        Returns {pool_address (bare), dex_pool_id (prefixed), token_address,
                 liquidity_usd, symbol, name} or None.
        """
        # 1. PRIMARY: /search/pools accepts either a token or pool address.
        try:
            data = self._request("/search/pools", params={"query": address, "network": self.network})
        except GeckoTerminalError as e:
            log.warning("search/pools failed for %s: %s", address, e)
            data = {"data": []}

        result = self._pick_best_solana_pool((data or {}).get("data", []) or [], address)
        if result:
            log.debug("resolved %s via search -> pool %s", address[:10], result["pool_address"][:10])
            return result

        # 2. FALLBACK A: treat as token mint.
        try:
            data = self._request(f"/networks/{self.network}/tokens/{address}/pools", params={"page": 1})
            result = self._pick_best_solana_pool((data or {}).get("data", []) or [], address)
            if result:
                log.debug("resolved %s via tokens endpoint", address[:10])
                return result
        except GeckoTerminalError:
            pass  # 404 means not a token; fall through to pool attempt

        # 3. FALLBACK B: treat as pool address directly.
        try:
            data = self._request(f"/networks/{self.network}/pools/{address}")
            result = self._pick_best_solana_pool((data or {}).get("data", []) or [], address)
            if result:
                log.debug("resolved %s via pools endpoint", address[:10])
                return result
        except GeckoTerminalError:
            pass

        log.info("could not resolve pool for address %s", address)
        return None

    # ---- OHLCV -------------------------------------------------------------

    def fetch_ohlcv(
        self,
        pool_address: str,
        token_address: str,
        start: datetime,
        end: datetime,
        aggregate: str = "hour",
    ) -> List[PricePoint]:
        """
        Fetch candles for [start, end). Cache-first; uncached spans are fetched
        from the API and stored.

        NOTE: GeckoTerminal's OHLCV param naming is inconsistent across
        endpoints — some pools accept "day/hour/minute/second", others accept
        "1/4/12". We try the string form first, then the numeric form, then a
        fallback. `aggregate` maps: "hour"="1", "minute"="second" etc.

        `pool_address` MUST be the bare address (no 'solana_' prefix).
        """
        pool_address = _strip_network_prefix(pool_address, self.network)
        # Try the REQUESTED aggregate first, then fall back to alternates if the
        # API rejects the value (aggregate-naming is inconsistent across pools).
        _all_aggs = ["minute", "hour", "1", "day"]
        aggregate_candidates = [aggregate] + [a for a in _all_aggs if a != aggregate]

        # 1. Try cache. Cache key includes the aggregate so minute/hour don't collide.
        agg_minutes_for_cache = {"minute": 1, "hour": 60, "day": 1440}.get(aggregate, 60)
        cached = cache_mod.load_candles(pool_address, token_address, start, end)
        expected = max(1, int((end - start).total_seconds() // 60 // agg_minutes_for_cache))
        if len(cached) >= int(expected * 0.8):
            return _fill_and_sort(cached, start, end)

        # 2. Fetch from API in chunks of up to 1000 candles.
        #    IMPORTANT: GeckoTerminal anchors results to before_timestamp and
        #    returns the NEWEST `limit` candles in the window — if the window
        #    holds more candles than the limit, the OLDEST (call-time!) candles
        #    get silently dropped. So each chunk must span FEWER candles than
        #    the limit (900 < 1000) to guarantee full coverage from the start.
        max_candle = 1000
        agg_minutes = {"minute": 1, "hour": 60, "day": 1440}.get(aggregate, 60)
        chunk_duration = timedelta(minutes=900 * agg_minutes)
        fetched: List[PricePoint] = []
        chunk_start = start
        safety = 0
        last_err = None
        while chunk_start < end and safety < 40:
            safety += 1
            chunk_end = min(end, chunk_start + chunk_duration)
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
                    break  # success
                except GeckoTerminalError as e:
                    last_err = e
                    msg = str(e)
                    # Only retry on aggregate-validation errors, not real 404s.
                    if "Invalid" in msg and ("aggregate" in msg or "timeframe" in msg):
                        continue
                    else:
                        break  # different error, stop trying aggregates
            if data is None:
                log.warning("ohlcv fetch failed for pool %s: %s", pool_address, last_err)
                break

            candles = _parse_ohlcv(data)
            if not candles:
                break
            fetched.extend(candles)
            latest = max(c.timestamp for c in candles)
            if latest <= chunk_start:
                break
            chunk_start = latest + timedelta(minutes=agg_minutes)

        if fetched:
            cache_mod.store_candles(pool_address, token_address, fetched)

        return _fill_and_sort(fetched, start, end)


# ---- module-level helpers ---------------------------------------------------


def _strip_network_prefix(pool_id: Optional[str], network: str) -> str:
    """Strip a leading 'solana_' (or other network) prefix from a pool id."""
    if not pool_id:
        return ""
    prefix = f"{network}_"
    if pool_id.startswith(prefix):
        return pool_id[len(prefix):]
    return pool_id


def _parse_retry_after(resp) -> Optional[float]:
    """Extract the Retry-After header value (seconds) if present."""
    val = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
    if not val:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _extract_base_token_address(pool_obj: dict, network: str) -> Optional[str]:
    """Pull the base token contract address out of a pool object."""
    # relationships.base_token.data.id is "<network>_<token_address>"
    rel = pool_obj.get("relationships", {}) or {}
    bt = (rel.get("base_token") or {}).get("data") or {}
    bt_id = bt.get("id")
    if bt_id:
        return _strip_network_prefix(bt_id, network)
    # Fallback: some shapes put it in attributes.
    attrs = pool_obj.get("attributes", {}) or {}
    return attrs.get("base_token", {}).get("address")


def _extract_token_symbol(pool_obj: dict) -> Optional[str]:
    """Best-effort token symbol from a pool object."""
    attrs = pool_obj.get("attributes", {}) or {}
    name = attrs.get("name") or ""
    # Names are usually "SYMBOL / SOL" — take the left half.
    if name and "/" in name:
        return name.split("/")[0].strip()
    bt = (attrs.get("base_token") or {})
    return bt.get("symbol")


def _parse_ohlcv(data: dict) -> List[PricePoint]:
    """
    GeckoTerminal OHLCV: each candle = [ts, o, h, l, c, v] (ts in seconds).

    Handles BOTH response shapes:
      - v1: data is a SINGLE dict {"data": {"attributes": {"ohlcv_list": [...]}}}
      - v3/list: data is a LIST of dicts [{"attributes": {"ohlcv_list": [...]}}, ...]
    """
    out: List[PricePoint] = []
    payload = (data or {}).get("data")
    # Normalize to a list of items.
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
                        open=float(o),
                        high=float(h),
                        low=float(l),
                        close=float(c),
                        volume=float(v) if v is not None else 0.0,
                    )
                )
            except (TypeError, ValueError, IndexError):
                continue
    return out


def _fill_and_sort(candles: List[PricePoint], start: datetime, end: datetime) -> List[PricePoint]:
    """Sort ascending and clamp to [start, end)."""
    if not candles:
        return []
    candles = [c for c in candles if start <= c.timestamp < end]
    candles.sort(key=lambda c: c.timestamp)
    return candles
