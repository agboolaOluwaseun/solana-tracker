"""
Strategy 3 — 50% TRAILING stop-loss (pure scorer, no I/O).

Position opens at `entry` (the open of the candle containing the call;
same convention family as the 7d engine). The stop floor starts at
entry/2 and can only RISE: every new high P lifts the floor to P/2.
Outcome over the candle window given:

  WIN      a candle's high touches 2x entry before the floor ever breaks.
  LOSS     a candle's low touches the floor that EARLIER candles formed —
           exit_multiple = floor/entry = (peak at that moment)/2, always
           in [0.5, 1.0): peak < 2x in every stop-out, so the exit is
           below entry — a real loss floored at -50%.
  EXPIRED  window ended with no 2x and no stop break (user rule 2026-09:
           expiry is NOT a verdict for stop strategies — undecided; shown
           gray in the UI with exit_multiple = last close / entry as its
           mark-to-market record, and excluded from every stat).

SAME-CANDLE RULE (user, inherited): a candle that touches 2x is a WIN no
matter what its low did (target checked first), mirroring which=same_candle
=> win in the 7d engine. Within a surviving candle we apply the engine's
hourly convention: the high leg raises the peak/floor BEFORE the low leg is
judged — a bar that spikes to 1.8x and collapses to 0.9x rides a stop that
rose to 0.9x and stops out (that IS 'gone -50% from the peak' measured
consecutively); it cannot dodge the trail it itself created.

WICK CLIP (Birdeye data lesson, 2026-09): a candle whose high towers over
its own body (high > 2*body_max) and is rejected by the NEXT candle's open
(< high/2) never offered that price to hold through — clip h to the body
envelope (mirror for crash-wicks that recover). This is exactly the
artifact that turned CALI's $0.0011 token into a 2000x minute. Clip only
touches h/l, never o/c.

Entry/call handling mirrors score_candles_stoploss: candles strictly
before the call's candle are ignored.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Sequence

from models import PricePoint, TrailingResult


def _span(series: Sequence[PricePoint]) -> timedelta:
    """Typical candle width — inferred from the first positive gap."""
    for i in range(1, min(len(series), 8)):
        d = series[i].timestamp - series[i - 1].timestamp
        if d.total_seconds() > 0:
            return d
    return timedelta(hours=1)


def _unpriceable(pool_address, error):
    return TrailingResult(
        entry_price_usd=None, peak_multiple=None, peak_price_usd=None,
        peak_timestamp=None, exit_multiple=None, exit_price_usd=None,
        exit_timestamp=None, loss_pct=None, hit_trailing_stop=False,
        is_win=False, status="unpriceable_loss", pool_address=pool_address,
        error=error)


def _clip_wicks(candles: Sequence[PricePoint]) -> list[PricePoint]:
    out = []
    n = len(candles)
    for i, c in enumerate(candles):
        o, h, lo, cl = c.open, c.high, c.low, c.close
        nxt = candles[i + 1].open if i + 1 < n else cl
        body_hi = max(o, cl, lo)
        if body_hi > 0 and h > body_hi * 2 and nxt < h * 0.5:
            h = body_hi
        body_lo = min(o, cl, h)
        if body_lo > 0 and lo < body_lo * 0.5 and nxt > body_lo * 2:
            lo = body_lo
        out.append(PricePoint(timestamp=c.timestamp, open=o, high=h, low=lo,
                              close=cl, volume=c.volume))
    return out


def score_candles_trailing(
    candles: Sequence[PricePoint],
    call_ts: datetime,
    *,
    win_multiplier: float = 2.0,
    trail_pct: float = 50.0,
    wick_clip: bool = True,
    pool_address: Optional[str] = None,
    entry_override: Optional[float] = None,
) -> TrailingResult:
    """Score one call on the 50% trailing-stop strategy. `candles` must be
    the 7d window series (hourly fine; minutes where cached).

    entry_override (user ratified 2026-09-24): the engine's canonical entry
    — calls.entry_price_usd, the dollar the normal + fixed-SL verdicts are
    measured from. Without it an hourly-only attach silently anchors on the
    HOUR open and can disagree with the call-minute open by 10x+ on a
    pumping token (691 trail rows did). When the curve's own anchor is >20x
    from the override the series belongs to another asset entirely (flipped
    pool, e.g. DRUGS' cached BIO curve) -> refuse, never score."""
    if not candles:
        return _unpriceable(pool_address, "no candles provided")

    series = sorted(candles, key=lambda c: c.timestamp)
    if wick_clip:
        series = _clip_wicks(series)

    span = _span(series)
    # entry = candle whose [ts, ts+span) contains the call; else the first
    # candle opening after it; else nothing we can honestly enter on.
    entry_c = next((c for c in series
                    if c.timestamp <= call_ts < c.timestamp + span), None)
    if entry_c is None:
        entry_c = next((c for c in series if c.timestamp > call_ts), None)
    if entry_c is None:
        return _unpriceable(pool_address, "no candles at/after call")
    curve_entry = entry_c.open
    if not curve_entry or curve_entry <= 0:
        return _unpriceable(pool_address, "zero entry price")
    entry = curve_entry
    entry_bar_neutralize = False
    if entry_override and entry_override > 0:
        ratio = curve_entry / entry_override
        if ratio > 20.0 or ratio * 20.0 < 1.0:
            # the stored dollar and this curve describe different assets
            # (flipped-pool cache, e.g. DRUGS' saved BIO curve) — refuse:
            # never measure a real entry against a fake series.
            return _unpriceable(
                pool_address,
                f"entry/curve mismatch x{curve_entry:.3e}/{entry_override:.3e}"
                f" = {ratio:.3g}")
        entry = float(entry_override)        # canonical: engine's stored dollar
        # The stored entry is a call-MINUTE price; if the entry bar opened
        # BEFORE the call (hourly granularity), its low includes pre-call
        # trading the follower never experienced — neutralize it (cannot
        # manufacture a stop-out from prices that predate the position).
        # The bar's HIGH still counts: a same-hour post-call peak is real,
        # and high-vs-target is checked before the stop (win bias, user
        # same-candle philosophy).
        entry_bar_neutralize = (entry_c.timestamp < call_ts
                                and (call_ts - entry_c.timestamp)
                                >= timedelta(minutes=2))

    trail_frac = 1.0 - trail_pct / 100.0        # floor as fraction of peak
    target = entry * win_multiplier

    path = [c for c in series if c.timestamp >= entry_c.timestamp]
    peak = entry                                # running peak
    peak_ts = entry_c.timestamp
    floor = entry * trail_frac                  # stop level, rises only
    hit_win = False
    loss_stop: Optional[tuple] = None           # (floor, ts) on stop-out

    for k, c in enumerate(path):
        # 1. 2x target FIRST — a candle that touches 2x is a win no matter
        #    what else it did (same-candle user rule: ties resolve for the
        #    caller, exactly like which=same_candle in the 7d engine).
        if c.high >= target:
            hit_win = True
            break
        # 2. high-leg updates the peak/floor BEFORE the low is judged —
        #    the engine's hourly convention (high-then-low inside a bar):
        #    a spike to 1.8x then collapse inside one hour rides a stop
        #    that rose to 0.9x, not the stale 0.5x.
        if c.high > peak:
            peak = c.high
            peak_ts = c.timestamp
            floor = max(floor, peak * trail_frac)
        # 3. stop-out on the current floor — EXCEPT on a re-anchored entry
        #    bar that opened before the call: its low includes pre-call
        #    trading the follower never experienced, so it can never
        #    manufacture a stop-out (high still counted above).
        if k == 0 and entry_bar_neutralize:
            continue
        if c.low <= floor:
            loss_stop = (floor, c.timestamp)
            break

    # win reports the FULL-window max (parity with normal + fixed-stop peak)
    full_peak = max((c.high for c in path), default=peak)

    if hit_win:
        peak = max(peak, full_peak)
        return TrailingResult(
            entry_price_usd=entry, peak_multiple=peak / entry,
            peak_price_usd=peak, peak_timestamp=peak_ts,
            exit_multiple=None, exit_price_usd=None, exit_timestamp=None,
            loss_pct=None, hit_trailing_stop=False, is_win=True,
            status="win", pool_address=pool_address, candles_used=len(path))

    if loss_stop is not None:
        stop, ts = loss_stop
        exit_mult = stop / entry                 # = peak/2 in entry units
        return TrailingResult(
            entry_price_usd=entry, peak_multiple=peak / entry,
            peak_price_usd=peak, peak_timestamp=peak_ts,
            exit_multiple=exit_mult, exit_price_usd=stop, exit_timestamp=ts,
            loss_pct=(exit_mult - 1.0) * 100.0, hit_trailing_stop=True,
            is_win=False, status="loss", pool_address=pool_address,
            candles_used=len(path))

    last = path[-1]
    exit_mult = last.close / entry               # held to the end — UNDECIDED
    return TrailingResult(
        entry_price_usd=entry, peak_multiple=full_peak / entry,
        peak_price_usd=full_peak, peak_timestamp=peak_ts,
        exit_multiple=exit_mult, exit_price_usd=last.close,
        exit_timestamp=last.timestamp,
        loss_pct=(exit_mult - 1.0) * 100.0, hit_trailing_stop=False,
        is_win=False, status="expired", pool_address=pool_address,
        candles_used=len(path))
