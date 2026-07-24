"""
Rate limiter for the GeckoTerminal API.

The free public API is capped at ~30 requests/minute and a paid key at ~250.
Since the user prioritises correctness over speed, this limiter deliberately
stays below the ceiling and pairs with exponential backoff (see
geckoterminal.py) so 429s never lose data.

Implementation: a monotonic sliding window. Before each request we block until
fewer than `rpm` calls have been made in the trailing 60 seconds. A small random
jitter is added to the wait so requests don't arrive at perfectly regular
intervals (helps avoid Cloudflare anti-bot heuristics).
"""
from __future__ import annotations

import random
import threading
import time
from collections import deque


class RateLimiter:
    def __init__(self, requests_per_minute: int):
        self.rpm = max(1, int(requests_per_minute))
        # Safety margin so transient bursts don't tip us into 429 territory.
        self._effective_rpm = max(1, self.rpm - 2)
        self._window = 60.0
        self._timestamps: "deque[float]" = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until a request slot is available, then record it."""
        with self._lock:
            now = time.monotonic()
            # Drop timestamps older than the trailing window.
            cutoff = now - self._window
            while self._timestamps and self._timestamps[0] <= cutoff:
                self._timestamps.popleft()

            if len(self._timestamps) >= self._effective_rpm:
                # Sleep until the oldest timestamp leaves the window, plus a
                # small random jitter so the cadence isn't perfectly regular.
                sleep_for = self._timestamps[0] + self._window - now
                if sleep_for > 0:
                    jitter = random.uniform(0.3, 1.5)
                    self._lock.release()
                    time.sleep(sleep_for + jitter)
                    self._lock.acquire()
                    # Re-prune after sleeping.
                    now = time.monotonic()
                    cutoff = now - self._window
                    while self._timestamps and self._timestamps[0] <= cutoff:
                        self._timestamps.popleft()

            self._timestamps.append(time.monotonic())

    @property
    def effective_rpm(self) -> int:
        return self._effective_rpm
