"""
Birdeye API client for Solana token pricing.

Used as an alternative pricing source when GeckoTerminal fails to return data.
Endpoints used:
  - GET /defi/ohlcv  — legacy OHLCV candles for a token
  - GET /defi/token/price  — spot price (fallback for entry)

Docs: https://docs.birdeye.so/reference/get-defi-ohlcv
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests

from config import settings
from models import PricePoint

log = logging.getLogger(__name__)

_BIRDEYE_BASE = "https://public-api.birdeye.so"
_BIRDEYE_HEADERS = {
    "accept": "application/json",
    "x-chain": "solana",
}

# Rate limiting: Birdeye free tier ~50k CU/month, OHLCV = 35 CU per request.
# Conservative throttle: 1 req/s with jitter.
_BIRDEYE_MIN_INTERVAL = 1.0


def _rate_limit(last_call: list[float]) -> None:
    """Simple rate limiter — wait if less than min_interval since last call."""
    now = time.time()
    if last_call:
        elapsed = now - last_call[-1]
        if elapsed < _BIRDEYE_MIN_INTERVAL:
            time.sleep(_BIRDEYE_MIN_INTERVAL - elapsed)
    last_call.append(time.time())


class BirdeyeError(Exception):
    pass


class BirdeyeClient:
    """Birdeye API client for Solana OHLCV data."""

    def __init__(self):
        self.session = requests.Session()
        key = settings.birdeye_api_key
        if not key:
            raise BirdeyeError("BIRDEYE_API_KEY not set in .env")
        self.session.headers.update({**_BIRDEYE_HEADERS, "X-API-KEY": key})
        self._last_call: list[float] = []

    def fetch_ohlcv(
        self,
        token_address: str,
        start: datetime,
        end: datetime,
        interval: str = "1m",
    ) -> List[PricePoint]:
        """
        Fetch OHLCV candles from Birdeye for a token over [start, end).

        Interval options: "1m", "5m", "15m", "30m", "1H", "4H", "1D", "1W".
        Max 1000 candles per request.

        Returns list of PricePoint objects sorted by timestamp.
        """
        _rate_limit(self._last_call)

        time_from = int(start.replace(tzinfo=timezone.utc).timestamp())
        time_to = int(end.replace(tzinfo=timezone.utc).timestamp())

        params = {
            "address": token_address,
            "type": interval,
            "time_from": time_from,
            "time_to": time_to,
        }

        log.info(
            "Birdeye OHLCV: %s [%s → %s] interval=%s",
            token_address[:10] + "…",
            start.isoformat(),
            end.isoformat(),
            interval,
        )

        resp = self.session.get(f"{_BIRDEYE_BASE}/defi/ohlcv", params=params, timeout=30)

        # Transient 429/5xx: retry a couple of times with backoff so a brief
        # throttle doesn't mark a real token unpriceable.
        for _attempt in range(2):
            if resp.status_code not in (429, 500, 502, 503, 504):
                break
            time.sleep(2.0 * (_attempt + 1))
            _rate_limit(self._last_call)
            resp = self.session.get(f"{_BIRDEYE_BASE}/defi/ohlcv", params=params, timeout=30)

        if resp.status_code != 200:
            raise BirdeyeError(
                f"HTTP {resp.status_code} from Birdeye: {resp.text[:300]}"
            )

        data = resp.json()
        if not data.get("success"):
            msg = data.get("message", "unknown error")
            raise BirdeyeError(f"Birdeye API error: {msg}")

        items = data.get("data", {}).get("items", [])
        candles = []
        for row in items:
            try:
                ts = datetime.fromtimestamp(int(row["unixTime"]), tz=timezone.utc).replace(tzinfo=None)
                candles.append(PricePoint(
                    timestamp=ts,
                    open=float(row["o"]),
                    high=float(row["h"]),
                    low=float(row["l"]),
                    close=float(row["c"]),
                    volume=float(row.get("vol", 0)),
                ))
            except (KeyError, TypeError, ValueError) as e:
                log.warning("Birdeye candle parse error: %s — row=%s", e, row)
                continue

        candles.sort(key=lambda c: c.timestamp)
        log.info("Birdeye: %d candles for %s", len(candles), token_address[:10] + "…")
        return candles

    def fetch_price(self, token_address: str) -> Optional[float]:
        """Fetch current spot price for a token."""
        _rate_limit(self._last_call)

        resp = self.session.get(
            f"{_BIRDEYE_BASE}/defi/token/price",
            params={"addresses": token_address},
            timeout=15,
        )

        if resp.status_code != 200:
            raise BirdeyeError(f"HTTP {resp.status_code} from Birdeye price: {resp.text[:200]}")

        data = resp.json()
        if not data.get("success"):
            return None

        token_data = data.get("data", {}).get(token_address)
        if token_data and "price" in token_data:
            return float(token_data["price"])
        return None
