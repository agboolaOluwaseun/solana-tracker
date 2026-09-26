"""
Fake-HIGH wick filter for hourly candles (user policy, 2026-09-24).

Lesson sources: museic's 6.88x (one 20:39 minute printed $2.002e-5, next
open $6.26e-6 — SpyDefi's real chart says x2.2, sustained minutes say
2.37x) and CALI's 12,590x (one 13:26 minute at $2.289 against $0.001
bodies). Both survived hourly scoring because nothing checked hourly
HIGH-vs-CLOSE absurdity outside the >=50x rescue guard.

The ratified two-tier rule — HIGHS ONLY (user's trading logic: a flash
LOW through a stop would have filled my sell order and must stand; a
flash HIGH never fills my hold):

  TIER 1 (hourly, free):  a suspect hour is one with high > hour_gate x
                          close (default 3.0). Everything else is kept
                          untouched with ZERO extra API — >=99% of hours.

  TIER 2 (that hour's minutes): pull [hour-3min, hour+63min) (padding so
                          edge minutes have neighbors). Any MINUTE with
                          high > min_gate x close (default 1.3) is a
                          fake print: replace its HIGH with the mean of
                          the two nearest existing neighbours' highs.
                          Edge rules (user): first candle of the series
                          -> mean of +1 and +2; last -> mean of -1 and
                          -2; a missing immediate neighbour -> the next
                          existing one on that side.

  DECISIONS:
   - no minute tripped -> the hour was a GENUINE intra-hour pump &
     dump; the original hourly candle is kept whole (its high really
     traded, just didn't hold to the close).
   - one or more trips -> rebuild the hour from the corrected minutes
     (o=first open, h=max corrected high, l=min low, c=last close).
     The rebuilt high may still exceed the gate — minute-verified now.
   - minutes unobtainable -> neighbour fallback (user):
     high := mean of the PREVIOUS and NEXT hourly highs; one-sided ->
     mean(neighbor high, own close); never RAISE a high; lows/opens/
     closes untouched.

Raw candles are never mutated: repair happens at READ time so the
function is idempotent and price_cache keeps the API's truth.

Wiring: strategy7d_cache.fetch_hourly (engine screening + all three
strategy walks, every rescore), pipeline._trailing_series (cache-only,
minute getters stay API-free there), birdeye_rescue hourly list. Gates
are config (WICK_HOUR_GATE / WICK_MIN_GATE / WICK_FILTER=false).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, List, Optional, Tuple

from models import PricePoint

MinuteGetter = Callable[[datetime, datetime], Tuple[List[PricePoint], int]]


def _gate_settings():
    """Live config read (monkeypatchable settings object)."""
    from config import settings
    on = getattr(settings, "wick_filter", True)
    hg = float(getattr(settings, "wick_hour_gate", 3.0))
    mg = float(getattr(settings, "wick_min_gate", 1.3))
    return on, hg, mg


def _neighbor_mean(highs: List[float], idx: int, n: int) -> Optional[float]:
    """Mean of the two nearest existing highs around idx (raw, un-clipped).
    One-sided edge (user rule): take the two available on the open side.
    Returns None when no two neighbours exist at all (single-candle
    series — leave the high alone)."""
    left: List[float] = []
    j = idx - 1
    while j >= 0 and len(left) < 2:
        if highs[j] > 0:
            left.append(highs[j])
        j -= 1
    right: List[float] = []
    j = idx + 1
    while j < n and len(right) < 2:
        if highs[j] > 0:
            right.append(highs[j])
        j += 1
    if left and right:
        return (left[0] + right[0]) / 2.0        # nearest on each side
    pool = left if len(right) < 2 else right     # edge: two from one side
    if len(pool) >= 2:
        return (pool[0] + pool[1]) / 2.0
    return None


def repair_minutes(mins: List[PricePoint], min_gate: Optional[float] = None
                   ) -> Tuple[List[PricePoint], int]:
    """Apply the minute-level half of the policy: clip fake-high minutes
    to their neighbours' mean high. Returns (corrected list, clipped_count).
    Lows are NEVER touched (user: flash lows must stand)."""
    if min_gate is None:
        _, _, min_gate = _gate_settings()
    n = len(mins)
    highs: List[float] = [float(m.high or 0.0) for m in mins]
    fixes = {i: _neighbor_mean(highs, i, n) for i, m in enumerate(mins)
             if m.high and m.close and m.close > 0 and m.high > min_gate * m.close}
    if not fixes:
        return list(mins), 0
    out = []
    for i, m in enumerate(mins):
        if i in fixes and fixes[i] is not None and fixes[i] < m.high:
            out.append(PricePoint(timestamp=m.timestamp, open=m.open,
                                  high=fixes[i], low=m.low, close=m.close,
                                  volume=m.volume))
        else:
            out.append(m)
    return out, len(fixes)


def _fallback_high(hourly: List[PricePoint], i: int) -> Optional[float]:
    """Minutes unobtainable: repair the hourly high from hourly neighbours
    (user rule). Never raises. None = nothing sane to repair with."""
    c = hourly[i]
    prev = hourly[i - 1].high if i > 0 else None
    nxt = hourly[i + 1].high if i + 1 < len(hourly) else None
    if prev and nxt:
        val = (prev + nxt) / 2.0
    elif nxt:
        val = (nxt + (c.close or nxt)) / 2.0
    elif prev:
        val = (prev + (c.close or prev)) / 2.0
    else:
        return None
    return min(val, c.high)          # only ever bring the high DOWN


def repair_hourly(hourly: List[PricePoint], get_minutes: MinuteGetter,
                  hour_gate: Optional[float] = None,
                  min_gate: Optional[float] = None) -> Tuple[List[PricePoint], int]:
    """Repair suspect hourly candles per the module policy. Returns
    (series, extra_api_calls). `hourly` must be sorted by timestamp.
    `get_minutes(start, end)` returns ([PricePoint], requests) — cache-first
    by convention; may hit the API for the ONE suspect hour only."""
    on, hg, mg = _gate_settings()
    if not on or not hourly:
        return list(hourly), 0
    if hour_gate:
        hg = hour_gate
    if min_gate:
        mg = min_gate
    out = list(hourly)
    reqs = 0
    span = timedelta(hours=1)
    pad = timedelta(minutes=3)
    for i, c in enumerate(out):
        if not c.close or c.close <= 0 or not c.high or c.high <= hg * c.close:
            continue                                   # innocent: free path
        start = c.timestamp
        try:
            mins, r = get_minutes(start - pad, start + span + pad)
        except Exception:  # noqa: BLE001 — API hiccup -> hourly fallback
            mins, r = [], 1
        reqs += r
        in_hour = [m for m in mins if start <= m.timestamp < start + span]
        if not in_hour:
            new_h = _fallback_high(out, i)
            if new_h is not None and new_h < c.high:
                out[i] = PricePoint(timestamp=c.timestamp, open=c.open,
                                    high=new_h, low=c.low, close=c.close,
                                    volume=c.volume)
            continue
        corrected, n_clipped = repair_minutes(in_hour, mg)
        if not n_clipped:
            continue                       # genuine pump&dump: keep hour whole
        out[i] = PricePoint(
            timestamp=start,
            open=corrected[0].open,
            high=max(m.high for m in corrected if m.high),
            low=min(m.low for m in corrected if m.low),
            close=corrected[-1].close,
            volume=sum(m.volume or 0.0 for m in corrected))
    return out, reqs
