"""
pricing/strategy7d.py — Pure, chain-agnostic 7-day token-call evaluation engine.

Frozen algorithm spec (contract — do not "improve"). Per call, all times naive UTC:

1. **Req 1 (hourly):** hourly OHLCV [call_hour_start, call_ts+eval_days*24h).
2. call_hour_candle = hourly candle with timestamp == call_hour_start. If absent ->
   unpriceable_loss, note 'no call-hour candle'.
3. **Screening:** screening_entry = call_hour_candle.low;
   screening_target = 2 * screening_entry;
   max_hourly_high = max(high of candles in [call_hour_start, end)).
   If max_hourly_high < screening_target -> DEFINITE LOSS (both strategies), stop,
   1 request.
4. **Req 2 (minute):** minute OHLCV [floor(call_ts to minute), call_hour_end).
5. **Entry:** candle at exact call minute; else candle containing call_ts; else
   OPTION 2: first minute candle with call_ts < candle.ts <= call_ts +
   entry_grace_minutes (flag option2_entry=True). If none or open <= 0 ->
   unpriceable_loss, note 'no call-minute candle'.
6. entry = entry_candle.open; target = 2*entry; stop = 0.5*entry.
7. **Final path (chronological):** minute candles >= entry_candle.ts in the call
   hour, then hourly candles [call_hour_end, end). The call-hour hourly candle is
   DISCARDED.
8. Walk the path; record first t2x (high >= target) and first t50 (low <= stop).
   - Both first appear in the SAME MINUTE candle -> which_first='same_candle' ->
     WIN for both strategies (user rule).
   - Both first appear in the SAME HOURLY candle -> REQ 3: minute candles for that
     hour; resolve order; same-minute rule re-applies; if minutes contradict the
     hour -> same_candle.
   - Else which_first = '2x' if t2x < t50 else 'minus50'.
9. Outcomes: plain = win iff t2x is not None.
   stoploss = win iff which_first in ('2x', 'same_candle').
10. Derived: max_price = max(high over path), min_price = min(low over path),
    max_multiple = max_price/entry, max_drawdown_pct = (1 - min_price/entry)*100,
    evaluation_end_timestamp = call_ts + eval_days*24h,
    granular_analysis_required = (req 2 happened).
11. Caching lives OUTSIDE this module (fetchers are injected; see
    pricing/strategy7d_cache.py for the aggregate-aware cached fetchers).

This module contains ONLY the algorithm: no globals, no DB, no network, no prints.
Source: validated prototype scripts/eval_strategy.py (666 run: 42W/40L/6 unp plain,
32W/50L/6 unp stoploss, 146 reqs over 88 calls).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional, Tuple

from models import PricePoint


@dataclass
class Eval7dResult:
    status_plain: str                 # 'win' | 'loss' | 'unpriceable_loss'
    status_stoploss: str              # same enum
    screening_entry_usd: Optional[float] = None
    screening_target_usd: Optional[float] = None
    entry_price_usd: Optional[float] = None
    target_usd: Optional[float] = None
    max_price_usd: Optional[float] = None
    min_price_usd: Optional[float] = None
    max_multiple: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    target_2x_reached: bool = False
    minus_50_reached: bool = False
    time_2x_reached: Optional[datetime] = None
    time_minus_50_reached: Optional[datetime] = None
    which_threshold_first: str = "none"   # '2x' | 'minus50' | 'same_candle' | 'none'
    api_requests_used: int = 0
    granular_analysis_required: bool = False
    option2_entry: bool = False
    evaluation_end_timestamp: Optional[datetime] = None
    pool_address: Optional[str] = None
    note: str = ""
    # Identity from pool resolution (token_meta/DexScreener) — applied to the
    # calls row so every priced call shows a real ticker, not a bare address.
    token_symbol: Optional[str] = None
    token_name: Optional[str] = None
    # Set when the Birdeye cross-chain rescue scored this call on a DIFFERENT
    # chain than the stored tag — apply_eval7d persists it so every later
    # pass resolves the pool on the right chain without needing Birdeye.
    chain_corrected: Optional[str] = None
    # LIVE mode (evaluate_call_7d given `now` inside the 7d window):
    # the walk covered [call, now) only. All statuses are then PROVISIONAL:
    # a screening 'loss' may still flip to win (and max_multiple grows) in
    # the days remaining. Frozen-spec outcomes are untouched when
    # window_complete=True (the default).
    window_complete: bool = True


def resolve_order_in_hour(
    fetch_minute: Callable[[str, str, datetime, datetime], Tuple[list, int]],
    pool: str,
    token: str,
    hour_candle: PricePoint,
    entry: float,
    target: float,
    stop: float,
) -> Tuple[str, int]:
    """REQUEST 3: minute data for one ambiguous hourly candle.
    Returns which_first: '2x' | 'minus50' | 'same_candle', plus reqs used.
    """
    h_start = hour_candle.timestamp.replace(minute=0, second=0, microsecond=0)
    h_end = h_start + timedelta(hours=1)
    minutes, reqs = fetch_minute(pool, token, h_start, h_end)
    t2x: Optional[datetime] = None
    t50: Optional[datetime] = None
    for m in minutes:
        if t2x is None and m.high >= target:
            t2x = m.timestamp
        if t50 is None and m.low <= stop:
            t50 = m.timestamp
        if t2x is not None and t50 is not None:
            break
    if t2x is None and t50 is None:
        # minute data contradicting the hourly candle — trust the hour,
        # treat as same-candle (user rule: win for all)
        return "same_candle", reqs
    if t2x is not None and t50 is not None and t2x == t50:
        return "same_candle", reqs
    if t2x is not None and (t50 is None or t2x < t50):
        return "2x", reqs
    return "minus50", reqs


def evaluate_call_7d(
    pool_address: Optional[str],
    token_address: str,
    call_ts: datetime,                 # naive UTC
    fetch_hourly: Callable[[str, str, datetime, datetime], Tuple[list, int]],
    fetch_minute: Callable[[str, str, datetime, datetime], Tuple[list, int]],
    eval_days: int = 7,
    entry_grace_minutes: int = 5,
    now: Optional[datetime] = None,    # LIVE mode: truncate the window at `now`
) -> Eval7dResult:
    """Run the frozen 7-day evaluation for one call.

    fetch_hourly / fetch_minute are injected callables
    (pool, token, start, end) -> (list[PricePoint], requests_used); the caller
    owns caching, rate limiting and the chain network.

    LIVE mode (`now` inside the 7-day window): the evaluation covers
    [call_ts, now) instead of [call_ts, call_ts+eval_days*24h). Outcomes are
    the same algorithm on the observed slice, but PROVISIONAL — screening
    cannot conclude 'definite loss' from an incomplete window, so a screening
    miss returns provisional_loss instead of final loss, and the minute deep-
    check still triggers whenever the observed high reaches the target. When
    `now` >= end7 (or None) the frozen full-window semantics apply unchanged.
    """
    end7 = call_ts + timedelta(days=eval_days)
    live = now is not None and now < end7
    window_end = now if (now is not None and live) else end7

    def _unpriceable(reqs: int, note: str, **kw) -> Eval7dResult:
        return Eval7dResult(
            status_plain="unpriceable_loss", status_stoploss="unpriceable_loss",
            api_requests_used=reqs, evaluation_end_timestamp=end7,
            pool_address=pool_address, note=note, window_complete=not live, **kw,
        )

    if not pool_address:
        return _unpriceable(0, "no pool")

    hour_start = call_ts.replace(minute=0, second=0, microsecond=0)
    hour_end = hour_start + timedelta(hours=1)
    minute_floor = call_ts.replace(second=0, microsecond=0)

    # ---- REQUEST 1: hourly [call_hour_start, window_end) ----
    try:
        hourly, reqs = fetch_hourly(pool_address, token_address, hour_start, window_end)
    except Exception as e:  # noqa: BLE001
        return _unpriceable(0, f"hourly fetch failed: {type(e).__name__}")

    call_hour_candle = next((c for c in hourly if c.timestamp == hour_start), None)
    if call_hour_candle is None:
        return _unpriceable(reqs, "no call-hour candle")

    # ---- screening test ----
    screening_entry = call_hour_candle.low
    screening_target = screening_entry * 2.0
    max_hourly_high = max(
        (c.high for c in hourly if hour_start <= c.timestamp < window_end), default=0.0
    )

    if max_hourly_high < screening_target:
        # Final only when the 7d window actually elapsed; live mode cannot
        # conclude a definite loss from an incomplete window.
        return Eval7dResult(
            status_plain="loss", status_stoploss="loss",
            screening_entry_usd=screening_entry, screening_target_usd=screening_target,
            which_threshold_first="none", api_requests_used=reqs,
            evaluation_end_timestamp=end7, pool_address=pool_address,
            note="screening LOSS" if not live else "provisional loss (window open)",
            window_complete=not live,
        )

    # ---- REQUEST 2: call-hour remainder minutes ----
    try:
        minutes, reqs2 = fetch_minute(pool_address, token_address, minute_floor, hour_end)
    except Exception as e:  # noqa: BLE001
        return _unpriceable(reqs, f"minute fetch failed: {type(e).__name__}",
                            screening_entry_usd=screening_entry,
                            screening_target_usd=screening_target)
    reqs += reqs2

    entry_candle = next((m for m in minutes if m.timestamp == minute_floor), None)
    if entry_candle is None:
        entry_candle = next(
            (m for m in minutes if m.timestamp <= call_ts < m.timestamp + timedelta(minutes=1)),
            None,
        )
    # OPTION 2 (user-approved): no candle at the exact call minute — the pool
    # started trading just after the call. Use the first available minute candle
    # within entry_grace_minutes AFTER the call as the entry (open), matching
    # what followers actually could have bought.
    option2 = False
    if entry_candle is None:
        for m in minutes:
            if call_ts < m.timestamp <= call_ts + timedelta(minutes=entry_grace_minutes):
                entry_candle = m
                option2 = True
                break
    if entry_candle is None or not entry_candle.open or entry_candle.open <= 0:
        return _unpriceable(reqs, "no call-minute candle",
                            screening_entry_usd=screening_entry,
                            screening_target_usd=screening_target)

    entry = entry_candle.open
    target = entry * 2.0
    stop = entry * 0.5

    # ---- final chronological path: call-hour minutes + post-hour hourlies ----
    path = [m for m in minutes if m.timestamp >= entry_candle.timestamp]
    # window_end (== now in live mode), never end7: a leaky fetcher must not
    # smuggle candles past the truncation point into a provisional verdict.
    path += [c for c in hourly if c.timestamp >= hour_end and c.timestamp < window_end]
    path.sort(key=lambda c: c.timestamp)

    t2x: Optional[datetime] = None
    t50: Optional[datetime] = None
    which_first = "none"
    for c in path:
        both_before = (t2x is None and t50 is None)
        if t2x is None and c.high >= target:
            t2x = c.timestamp
        if t50 is None and c.low <= stop:
            t50 = c.timestamp
        if both_before and t2x is not None and t50 is not None:
            # first occurrence of BOTH thresholds in THIS candle
            if c.timestamp < hour_end:
                # minute candle -> same-candle win for all (user rule)
                which_first = "same_candle"
            else:
                # hourly candle -> REQUEST 3 to resolve the hour
                which_first, reqs3 = resolve_order_in_hour(
                    fetch_minute, pool_address, token_address, c, entry, target, stop
                )
                reqs += reqs3
            break
        if t2x is not None and t50 is not None:
            which_first = "2x" if t2x < t50 else "minus50"
            break

    if which_first == "none":
        if t2x is not None:
            which_first = "2x"
        elif t50 is not None:
            which_first = "minus50"

    plain = "win" if t2x is not None else "loss"
    if t2x is not None and which_first == "same_candle":
        stoploss = "win"                      # user rule
    elif which_first == "2x":
        stoploss = "win"
    else:
        stoploss = "loss"

    max_price = max((c.high for c in path), default=entry)
    min_price = min((c.low for c in path), default=entry)

    return Eval7dResult(
        status_plain=plain, status_stoploss=stoploss,
        screening_entry_usd=screening_entry, screening_target_usd=screening_target,
        entry_price_usd=entry, target_usd=target,
        max_price_usd=max_price, min_price_usd=min_price,
        max_multiple=max_price / entry if entry else None,
        max_drawdown_pct=(1.0 - min_price / entry) * 100 if entry else None,
        target_2x_reached=t2x is not None,
        minus_50_reached=t50 is not None,
        time_2x_reached=t2x,
        time_minus_50_reached=t50,
        which_threshold_first=which_first,
        api_requests_used=reqs,
        granular_analysis_required=True,
        option2_entry=option2,
        evaluation_end_timestamp=end7,
        pool_address=pool_address,
        note=which_first,
        window_complete=not live,
    )
