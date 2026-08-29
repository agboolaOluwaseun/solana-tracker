"""
Adaptive rate limiter for the GeckoTerminal API.

Empirical findings (measured against the live API, 2026-08):
  - The "Cloudflare protection" is actually GeckoTerminal/CoinGecko's own
    rolling-window rate limiter sitting behind Cloudflare's edge cache.
  - Throttle signal is HTTP 429 with `Retry-After: 0`. The header value is
    UNRELIABLE (often 0 even when the window hasn't reset), so we must NOT
    trust it literally.
  - Identical URLs are served from Cloudflare's edge cache (`cf-cache-status:
    HIT`) and do NOT count against the origin limit.
  - Throttling was observed to trigger at ~23 effective RPM, above the
    advertised ~20. Sustained throughput must therefore stay ~35% BELOW the
    ceiling, not at it.
  - Recovery is short: after ~8s the origin returns 200 again. So a modest
    cooldown, growing only if throttling persists, is the right recovery.

Design (reliability-first, not max-RPM):
  - Sustained target = advertised RPM * safety_margin (default 0.65) so we
    never approach the real ceiling during normal operation.
  - Sliding-window gate keeps the sustained rate stable and jittered so the
    cadence isn't perfectly regular (avoids anti-bot heuristics).
  - A circuit breaker opens on any 429: all acquires block until a cooldown
    elapses (grows exponentially with consecutive 429s, capped). It closes
    after a run of clean successes, restoring the normal rate.
  - notify_throttled()/notify_success() let the caller feed throttle signals
    back so the limiter self-adjusts instead of hammering the boundary.
"""
from __future__ import annotations

import random
import threading
import time
from collections import deque


class RateLimiter:
    def __init__(
        self,
        requests_per_minute: int,
        safety_margin: float = 0.65,
        min_rpm: int = 6,
    ):
        self._advertised_rpm = max(1, int(requests_per_minute))
        # Sustained target stays safely below the real ceiling.
        self._target_rpm = max(min_rpm, int(self._advertised_rpm * safety_margin))
        self._min_rpm = min_rpm
        self._window = 60.0
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()
        # Pacing: enforce a minimum gap between requests so even the in-window
        # burst is spread out smoothly instead of arriving as a tight cluster
        # (tight clusters are what anti-bot heuristics flag).
        self._min_interval = self._window / max(self._target_rpm, 1)
        self._last_request_at = 0.0

        # Circuit breaker state.
        self._cooldown_until = 0.0          # monotonic ts; acquire() blocks till then
        self._consecutive_429 = 0
        self._clean_since_429 = 0
        self._min_cooldown = 20.0           # first 429 -> 20s
        self._max_cooldown = 300.0          # cap the exponential growth
        self._cooldown_growth = 2.0         # double each consecutive 429
        self._clean_reset_threshold = 8     # N clean successes -> reset breaker

    # ---- public API -------------------------------------------------------

    def acquire(self) -> None:
        """Block until a request slot is available (respecting the breaker)."""
        with self._lock:
            # Circuit breaker: if throttled, wait out the cooldown.
            now = time.monotonic()
            if now < self._cooldown_until:
                wait = self._cooldown_until - now
                self._lock.release()
                time.sleep(wait)
                self._lock.acquire()

            # Pacing: never fire two requests closer than the target interval,
            # even if the sliding window would allow it. This kills the initial
            # burst-of-RPM pattern that anti-bot heuristics flag.
            now = time.monotonic()
            if self._last_request_at and now - self._last_request_at < self._min_interval:
                pad = self._min_interval - (now - self._last_request_at)
                jitter = random.uniform(0.3, 1.5)
                self._lock.release()
                time.sleep(pad + jitter)
                self._lock.acquire()

            # Sliding window gate at the sustained target.
            now = time.monotonic()
            cutoff = now - self._window
            while self._timestamps and self._timestamps[0] <= cutoff:
                self._timestamps.popleft()

            if len(self._timestamps) >= self._target_rpm:
                sleep_for = self._timestamps[0] + self._window - now
                if sleep_for > 0:
                    jitter = random.uniform(0.3, 1.5)
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
        """Called on a 429. Opens the circuit breaker and lowers the rate.

        retry_after_hint is deliberately treated as advisory only — the live
        API returns `Retry-After: 0` even when the window hasn't reset, so we
        use our own growing cooldown and only consult the hint if it is larger
        than our floor.
        """
        with self._lock:
            self._consecutive_429 += 1
            self._clean_since_429 = 0
            # Exponential cooldown, capped.
            cooldown = self._min_cooldown * (
                self._cooldown_growth ** (self._consecutive_429 - 1)
            )
            cooldown = min(cooldown, self._max_cooldown)
            # Honor a larger server hint if present, but never below our floor.
            hint = (retry_after_hint or 0.0)
            if hint > cooldown:
                cooldown = min(hint, self._max_cooldown)
            self._cooldown_until = time.monotonic() + cooldown
            # Drop the sustained rate so we don't re-trip immediately on resume.
            self._target_rpm = max(
                self._min_rpm, int(self._target_rpm * 0.6)
            )

    def notify_success(self) -> None:
        """Called on a healthy response. Slowly restores the normal rate."""
        with self._lock:
            if self._consecutive_429 == 0:
                return
            self._clean_since_429 += 1
            if self._clean_since_429 >= self._clean_reset_threshold:
                self._consecutive_429 = 0
                self._clean_since_429 = 0
                self._cooldown_until = 0.0
                # Restore the sustained target toward the advertised ceiling.
                self._target_rpm = max(
                    self._min_rpm, int(self._advertised_rpm * 0.65)
                )

    # ---- introspection ----------------------------------------------------

    @property
    def target_rpm(self) -> int:
        return self._target_rpm

    @property
    def advertised_rpm(self) -> int:
        return self._advertised_rpm

    @property
    def in_cooldown(self) -> bool:
        return time.monotonic() < self._cooldown_until