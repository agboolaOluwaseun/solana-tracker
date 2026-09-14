"""
dexscreener.py — FREE pool resolution via DexScreener (no API key, no rate limit issues).

Why this exists: GeckoTerminal's global rate limit (~20 RPM observed, safe ~12 RPM)
is shared between pool resolution and OHLCV. DexScreener resolves the same
highest-liquidity Solana pool from a token mint — verified 6/6 identical results
against GeckoTerminal's resolve_pool_smart on real tracked tokens — at
300 req/min with no key.

Using DexScreener for resolution frees the ENTIRE GeckoTerminal budget for
OHLCV candles: a cold call drops from ~3 GeckoTerminal requests to 1.

Endpoint: GET https://api.dexscreener.com/latest/dex/tokens/{mint}
Returns all pairs for the mint; we pick the highest-USD-liquidity Solana pair.
The pair address IS the pool address GeckoTerminal's OHLCV endpoint expects.

Read-only against our database: resolution results are returned to the caller;
storage is handled by pipeline_v2 (same upsert path as before).
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

_BASE = "https://api.dexscreener.com"

# DexScreener allows 300 req/min; pace ourselves at ~2/s to stay far under it.
_MIN_INTERVAL = 0.5


class DexScreenerError(Exception):
    pass


class DexScreenerClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        })
        self._last_call = 0.0

    def _pace(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)
        self._last_call = time.monotonic()

    def resolve_pool(self, token_address: str, chain: str = "solana") -> Optional[dict]:
        """
        Resolve a token mint to its highest-liquidity pool on `chain`
        ('solana' | 'robinhood' — DexScreener chainId values).

        Returns a dict in the same shape GeckoTerminalClient.resolve_pool_smart
        produces: {pool_address, dex_pool_id, token_address, liquidity_usd,
        symbol, name} — or None if the token has no live pair on that chain.
        On Robinhood the pairAddress IS the GeckoTerminal pool key.
        """
        self._pace()
        try:
            resp = self.session.get(
                f"{_BASE}/latest/dex/tokens/{token_address}", timeout=20
            )
        except requests.RequestException as e:
            log.warning("DexScreener request failed for %s: %s", token_address[:10], e)
            return None

        if resp.status_code != 200:
            log.debug("DexScreener HTTP %s for %s", resp.status_code, token_address[:10])
            return None

        try:
            pairs = (resp.json() or {}).get("pairs") or []
        except ValueError:
            return None

        chain_pairs = [p for p in pairs if p.get("chainId") == chain]
        if not chain_pairs:
            # The queried address may be a POOL (channels that post calls as
            # dexscreener/geckoterminal links): look the pair up directly.
            pool_pair = self.resolve_by_pool(token_address, chain)
            return pool_pair

        # Same selection rule GeckoTerminal uses: highest USD liquidity.
        chain_pairs.sort(
            key=lambda p: (p.get("liquidity") or {}).get("usd") or 0, reverse=True
        )
        best = chain_pairs[0]
        pool_address = best.get("pairAddress")
        if not pool_address:
            return None

        base = best.get("baseToken") or {}
        return {
            "pool_address": pool_address,
            "dex_pool_id": pool_address,
            # Use the queried mint (DexScreener may flip base/quote orientation);
            # callers treat this as the token being priced.
            "token_address": token_address,
            "liquidity_usd": (best.get("liquidity") or {}).get("usd") or 0,
            "symbol": base.get("symbol"),
            "name": base.get("name"),
        }

    def resolve_by_pool(self, pair_address: str, chain: str = "solana") -> Optional[dict]:
        """Resolve by the PAIR (pool) address itself — the address embedded in
        dexscreener/geckoterminal links channels like Civilian Degens post.

        Uses the /latest/dex/search endpoint (finds pairs by pool OR token
        address) and keeps only a chain-matching pair whose pairAddress is
        the queried one (or whose base token matches, if search returns by
        token). Returns the same dict shape as resolve_pool, with token_address
        set to the pair's BASE token (the thing actually priced).
        """
        self._pace()
        try:
            resp = self.session.get(
                f"{_BASE}/latest/dex/search", params={"q": pair_address}, timeout=20
            )
            if resp.status_code != 200:
                return None
            pairs = (resp.json() or {}).get("pairs") or []
        except (requests.RequestException, ValueError):
            return None

        want = pair_address.lower()
        best = None
        for p in pairs:
            if p.get("chainId") != chain:
                continue
            pa = (p.get("pairAddress") or "").lower()
            bt = ((p.get("baseToken") or {}).get("address") or "").lower()
            if pa == want or bt == want:
                if best is None or (p.get("liquidity") or {}).get("usd", 0) > \
                        (best.get("liquidity") or {}).get("usd", 0):
                    best = p
        if not best:
            return None
        base = best.get("baseToken") or {}
        pool_address = best.get("pairAddress")
        if not pool_address:
            return None
        return {
            "pool_address": pool_address,
            "dex_pool_id": pool_address,
            "token_address": base.get("address") or pair_address,
            "liquidity_usd": (best.get("liquidity") or {}).get("usd") or 0,
            "symbol": base.get("symbol"),
            "name": base.get("name"),
        }
