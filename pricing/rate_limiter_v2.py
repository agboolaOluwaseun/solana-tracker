"""
Rate limiter v2 — investigation file for optimizing GeckoTerminal throughput.

Current bottleneck analysis (2026-08):
- 142 calls took 3+ hours
- Each call needs: pool resolution (1 req) + OHLCV fetch (1-2 reqs) = ~284 requests
- Current limiter: 20 RPM advertised × 0.65 safety = 13 RPM sustained
- Plus jitter 0.4-1.8s per request
- Plus circuit breaker cooldowns (20s min, doubles on each 429)

Theoretical minimum: 284 reqs / 13 RPM = 21.8 minutes
Actual: 3+ hours = 10x slower than theoretical

Root causes:
1. Safety margin 0.65 is too conservative (13 RPM vs 20 RPM ceiling)
2. Jitter 0.4-1.8s adds ~1s average per request = 284s extra
3. Circuit breaker cooldowns are aggressive (20s min, doubles)
4. Pool resolution failures trigger retries with backoff

This file experiments with:
- Higher safety margin (0.85 → 17 RPM)
- Reduced jitter (0.1-0.5s)
- Shorter circuit breaker cooldown (8s min, 1.5x growth)
- Adaptive rate: start conservative, increase on success
"""
from __future__ import annotations

import random
import threading
import time
from collections import deque


class RateLimiterV2:
    """Optimized rate limiter with adaptive behavior.

    AGGRESSIVE MODE (authorized by user: "burn the rate budget"):
      - Targets the server ceiling (~14 RPM for OHLCV) instead of sitting 40% under it.
      - Very fast circuit-breaker recovery: a 429 costs a short cooldown, then the
        rate snaps straight back instead of de-escalating into a death spiral.
      - The ceiling is enforced server-side per IP; the point of this mode is to
        spend the full budget and recover from throttles in seconds, not avoid them.
    """

    def __init__(
        self,
        requests_per_minute: int,
        safety_margin: float = 1.0,  # spend the full budget
        min_rpm: int = 4,
        initial_jitter_range: tuple[float, float] = (0.05, 0.3),  # tight pacing
    ):
        self._advertised_rpm = max(1, int(requests_per_minute))
        self._target_rpm = max(min_rpm, int(self._advertised_rpm * safety_margin))
        self._min_rpm = min_rpm
        self._window = 60.0
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

        # Pacing
        self._min_interval = self._window / max(self._target_rpm, 1)
        self._last_request_at = 0.0
        self._jitter_range = initial_jitter_range

        # Circuit breaker — aggressive/fast-recovery
        self._cooldown_until = 0.0
        self._consecutive_429 = 0
        self._clean_since_429 = 0
        self._min_cooldown = 4.0        # short: recover in seconds
        self._max_cooldown = 30.0       # never spiral into minutes
        self._cooldown_growth = 1.3     # gentle growth
        self._clean_reset_threshold = 2 # snap back after just 2 clean requests

        # Adaptive rate tracking
        self._success_count = 0
        self._throttle_count = 0
        self._rate_boost_applied = False
    
    def acquire(self) -> None:
        """Block until a request slot is available."""
        with self._lock:
            # Circuit breaker
            now = time.monotonic()
            if now < self._cooldown_until:
                wait = self._cooldown_until - now
                self._lock.release()
                time.sleep(wait)
                self._lock.acquire()
            
            # Pacing with reduced jitter
            now = time.monotonic()
            if self._last_request_at and now - self._last_request_at < self._min_interval:
                pad = self._min_interval - (now - self._last_request_at)
                jitter = random.uniform(*self._jitter_range)
                self._lock.release()
                time.sleep(pad + jitter)
                self._lock.acquire()
            
            # Sliding window
            now = time.monotonic()
            cutoff = now - self._window
            while self._timestamps and self._timestamps[0] <= cutoff:
                self._timestamps.popleft()
            
            if len(self._timestamps) >= self._target_rpm:
                sleep_for = self._timestamps[0] + self._window - now
                if sleep_for > 0:
                    jitter = random.uniform(*self._jitter_range)
                    self._lock.release()
                    time.sleep(sleep_for + jitter)
                    self._lock.acquire()
                    now = time.monotonic()
                    cutoff = now - self._window
                    while self._timestamps and self._timestamps[0] <= cutoff:
                        self._timestamps.popleft()
            
            self._timestamps.append(time.monotonic())
            self._last_request_at = time.monotonic()
    
    def notify_throttled(self, retry_after_hint: float | None = None) -> None:
        """Called on 429. AGGRESSIVE: short cooldown, no permanent rate drop."""
        with self._lock:
            self._consecutive_429 += 1
            self._clean_since_429 = 0
            self._throttle_count += 1

            # Short cooldown only — we do NOT lower the sustained rate. The
            # ceiling is enforced server-side per IP; dropping the client rate
            # just leaves budget on the table after the window resets.
            cooldown = self._min_cooldown * (
                self._cooldown_growth ** (self._consecutive_429 - 1)
            )
            cooldown = min(cooldown, self._max_cooldown)

            hint = retry_after_hint or 0.0
            if hint > cooldown:
                cooldown = min(hint, self._max_cooldown)

            self._cooldown_until = time.monotonic() + cooldown
            # NOTE: target_rpm intentionally untouched — spend the full budget.
    
    def notify_success(self) -> None:
        """Called on success. AGGRESSIVE: snap straight back to full rate."""
        with self._lock:
            self._success_count += 1

            if self._consecutive_429 == 0:
                return

            self._clean_since_429 += 1
            if self._clean_since_429 >= self._clean_reset_threshold:
                self._consecutive_429 = 0
                self._clean_since_429 = 0
                self._cooldown_until = 0.0
                # Full budget restored immediately.
                self._target_rpm = max(
                    self._min_rpm, int(self._advertised_rpm)
                )
                self._min_interval = self._window / max(self._target_rpm, 1)
    
    @property
    def target_rpm(self) -> int:
        return self._target_rpm
    
    @property
    def advertised_rpm(self) -> int:
        return self._advertised_rpm
    
    @property
    def in_cooldown(self) -> bool:
        return time.monotonic() < self._cooldown_until
    
    def get_stats(self) -> dict:
        """Return current limiter statistics."""
        return {
            "target_rpm": self._target_rpm,
            "advertised_rpm": self._advertised_rpm,
            "in_cooldown": self.in_cooldown,
            "success_count": self._success_count,
            "throttle_count": self._throttle_count,
            "rate_boost_applied": self._rate_boost_applied,
        }
