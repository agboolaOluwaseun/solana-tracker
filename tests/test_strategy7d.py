"""Synthetic-candle unit tests for the frozen 7-day evaluation engine.

No DB, no network: injected fetchers return hand-built PricePoint lists.
One test per scenario row in the integration plan (Workstream A, Task A2).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from models import PricePoint
from pricing.strategy7d import evaluate_call_7d

# 2026-04-01 12:34:00 UTC — call inside hour 12:00-13:00
CALL_TS = datetime(2026, 4, 1, 12, 34, 0)
HOUR_START = datetime(2026, 4, 1, 12, 0, 0)
HOUR_END = datetime(2026, 4, 1, 13, 0, 0)

POOL = "poolXYZ"
TOKEN = "***"


def pp(ts, o, h, l, c=None, v=0.0):
    return PricePoint(timestamp=ts, open=o, high=h, low=l,
                      close=c if c is not None else o, volume=v)


def hourly(ts, h, l):
    """Plain hourly candle (open/close unused by the algorithm)."""
    return pp(ts, l, h, l)


def make_fetchers(hourly_candles, minute_candles, leak_hourly_before=False):
    """Fetchers that serve candles inside the requested [start, end) —
    mimicking GT's windowed response. Each call costs exactly 1 request.
    leak_hourly_before=True also returns hourly candles from BEFORE start
    (GT commonly returns the latest N ending before `end`, so the engine's own
    window filters must stay defensive)."""
    def fetch_hourly(pool, token, start, end):
        if leak_hourly_before:
            out = [c for c in hourly_candles if c.timestamp < end]
        else:
            out = [c for c in hourly_candles if start <= c.timestamp < end]
        out.sort(key=lambda c: c.timestamp)
        return out, 1

    def fetch_minute(pool, token, start, end):
        out = sorted((m for m in minute_candles if start <= m.timestamp < end),
                     key=lambda m: m.timestamp)
        return out, 1

    return fetch_hourly, fetch_minute


def evaluate(hourly_candles, minute_candles, **fetcher_kwargs):
    fh, fm = make_fetchers(hourly_candles, minute_candles, **fetcher_kwargs)
    return evaluate_call_7d(POOL, TOKEN, CALL_TS, fh, fm)


# call-hour candle helper: screening target = 2*low
CH_LO = 1.0                      # screening entry -> screening target 2.0


# --------------------------------------------------------------------------
# 1 — hourly highs never reach 2x call-hour LOW -> screening LOSS, 1 request
# --------------------------------------------------------------------------
def test_screening_loss_one_request():
    hours = [
        hourly(HOUR_START, 1.5, CH_LO),
        hourly(HOUR_END, 1.6, 1.0),
        hourly(HOUR_END + timedelta(hours=1), 1.7, 1.1),
    ]
    r = evaluate(hours, [])
    assert r.status_plain == "loss"
    # user rule 2026-09: the -50% stop never broke (lows >= 1.0 > 0.5 floor)
    # -> the STOP strategy has no verdict at all: 'expired' (undecided, gray).
    assert r.status_stoploss == "expired"
    assert r.final_close_usd == 1.1        # mark-to-market for the tile
    assert r.api_requests_used == 1
    assert r.granular_analysis_required is False
    assert r.note == "screening LOSS"
    assert r.screening_entry_usd == CH_LO and r.screening_target_usd == 2.0


# --------------------------------------------------------------------------
# 2 — screening passes, 2x at hour 3, no dip -> win/win, which_first '2x'
# --------------------------------------------------------------------------
def test_clean_win():
    hours = [
        hourly(HOUR_START, 1.5, CH_LO),
        hourly(HOUR_END, 1.5, 1.0),
        hourly(HOUR_END + timedelta(hours=1), 1.6, 1.0),
        hourly(HOUR_END + timedelta(hours=2), 3.0, 1.0),   # t2x (target 2.4)
    ]
    mins = [pp(CALL_TS.replace(second=0), 1.2, 1.3, 1.1)]
    r = evaluate(hours, mins)
    assert r.status_plain == "win" and r.status_stoploss == "win"
    assert r.which_threshold_first == "2x"
    assert r.api_requests_used == 2
    assert r.time_2x_reached == HOUR_END + timedelta(hours=2)
    assert r.time_minus_50_reached is None
    assert r.granular_analysis_required is True


# --------------------------------------------------------------------------
# 3 — -50% at hour 1, 2x at hour 5 -> plain win, stoploss loss
# --------------------------------------------------------------------------
def test_dip_before_pump():
    hours = [
        hourly(HOUR_START, 1.5, CH_LO),
        hourly(HOUR_END, 1.5, 0.5),                          # t50 (stop 0.6)
        hourly(HOUR_END + timedelta(hours=2), 1.6, 0.8),
        hourly(HOUR_END + timedelta(hours=4), 3.0, 0.9),     # t2x (target 2.4)
    ]
    mins = [pp(CALL_TS.replace(second=0), 1.2, 1.3, 1.1)]
    r = evaluate(hours, mins)
    assert r.status_plain == "win" and r.status_stoploss == "loss"
    assert r.which_threshold_first == "minus50"
    assert r.api_requests_used == 2


# --------------------------------------------------------------------------
# 4 — 2x and -50% in the SAME minute candle -> win for all strategies
# --------------------------------------------------------------------------
def test_same_minute_candle_is_win():
    hours = [
        hourly(HOUR_START, 5.0, CH_LO),                      # screening pass
        hourly(HOUR_END, 1.2, 1.0),
    ]
    # entry 1.2 -> target 2.4, stop 0.6: one minute breaches both
    mins = [pp(CALL_TS.replace(second=0), 1.2, 2.5, 0.6)]
    r = evaluate(hours, mins)
    assert r.which_threshold_first == "same_candle"
    assert r.status_plain == "win" and r.status_stoploss == "win"
    assert r.api_requests_used == 2


# --------------------------------------------------------------------------
# 5 — both thresholds first inside one LATER hourly; minutes show 2x first
# --------------------------------------------------------------------------
def test_later_hour_resolved_2x_first():
    hours = [
        hourly(HOUR_START, 2.5, CH_LO),                      # screening pass
        hourly(HOUR_END, 1.5, 0.8),                          # no hit (entry 1.0)
        hourly(HOUR_END + timedelta(hours=1), 2.5, 0.4),     # BOTH -> req 3
    ]
    mins = [pp(CALL_TS.replace(second=0), 1.0, 1.2, 0.9)]    # entry 1.0, target 2.0, stop 0.5
    # req-3 minutes for hour 14:00: 2x at 14:05 before -50 at 14:20
    mins += [
        pp(datetime(2026, 4, 1, 14, 5), 1.0, 2.5, 1.0),
        pp(datetime(2026, 4, 1, 14, 20), 1.0, 1.2, 0.4),
    ]
    r = evaluate(hours, mins)
    assert r.which_threshold_first == "2x"
    assert r.status_plain == "win" and r.status_stoploss == "win"
    assert r.api_requests_used == 3


# --------------------------------------------------------------------------
# 6 — same as 5 but minutes show -50 first -> stoploss loss
# --------------------------------------------------------------------------
def test_later_hour_resolved_minus50_first():
    hours = [
        hourly(HOUR_START, 2.5, CH_LO),
        hourly(HOUR_END, 1.5, 0.8),
        hourly(HOUR_END + timedelta(hours=1), 2.5, 0.4),     # BOTH -> req 3
    ]
    mins = [pp(CALL_TS.replace(second=0), 1.0, 1.2, 0.9)]
    mins += [  # -50 at 14:02 before 2x at 14:30
        pp(datetime(2026, 4, 1, 14, 2), 0.9, 0.9, 0.4),
        pp(datetime(2026, 4, 1, 14, 30), 1.0, 2.5, 1.0),
    ]
    r = evaluate(hours, mins)
    assert r.which_threshold_first == "minus50"
    assert r.status_plain == "win" and r.status_stoploss == "loss"
    assert r.api_requests_used == 3


# --------------------------------------------------------------------------
# 7 — no minute candle at call minute; first candle 3 min after -> Option 2
# --------------------------------------------------------------------------
def test_option2_entry():
    hours = [
        hourly(HOUR_START, 1.5, CH_LO),
        hourly(HOUR_END, 1.4, 1.0),
        hourly(HOUR_END + timedelta(hours=1), 3.0, 1.0),     # t2x
    ]
    mins = [
        pp(datetime(2026, 4, 1, 12, 37), 1.2, 1.3, 1.1),     # 3 min after call
        pp(datetime(2026, 4, 1, 12, 38), 1.2, 1.25, 1.15),
    ]
    r = evaluate(hours, mins)
    assert r.option2_entry is True
    assert r.entry_price_usd == 1.2
    assert r.status_plain == "win"
    assert r.api_requests_used == 2


# --------------------------------------------------------------------------
# 8 — no minute candle within 5 min after call -> unpriceable
# --------------------------------------------------------------------------
def test_no_entry_candle_in_grace():
    hours = [
        hourly(HOUR_START, 5.0, CH_LO),
        hourly(HOUR_END, 3.0, 1.0),
    ]
    mins = [pp(datetime(2026, 4, 1, 12, 45), 1.2, 1.3, 1.1)]  # 11 min after call
    r = evaluate(hours, mins)
    assert r.status_plain == "unpriceable_loss"
    assert r.note == "no call-minute candle"
    assert r.api_requests_used == 2


# --------------------------------------------------------------------------
# 9 — no hourly candle at call hour -> unpriceable, 1 request spent
# --------------------------------------------------------------------------
def test_no_call_hour_candle():
    hours = [hourly(HOUR_END, 9.0, 1.0)]                     # starts AFTER call hour
    r = evaluate(hours, [])
    assert r.status_plain == "unpriceable_loss"
    assert r.note == "no call-hour candle"
    assert r.api_requests_used == 1


# --------------------------------------------------------------------------
# 10 — pre-call spike can never create a win (outside fetch window)
# --------------------------------------------------------------------------
def test_pre_call_spike_ignored():
    # leak mode: fetcher hands back the pre-call candle too, so the engine's
    # own [hour_start, end7) filters are the only defense.
    hours = [
        hourly(HOUR_START - timedelta(hours=1), 50.0, 5.0),  # huge PRE-call spike
        hourly(HOUR_START, 1.5, CH_LO),                      # call hour: high 1.5
        hourly(HOUR_END, 1.5, 1.0),                          # all < screening target 2.0
        hourly(HOUR_END + timedelta(hours=1), 1.6, 1.1),
    ]
    r = evaluate(hours, [], leak_hourly_before=True)
    # pre-call spike ignored; nothing in-window breaks the 0.5 floor either
    # -> plain loss, stop strategy expired (user rule 2026-09).
    assert r.status_plain == "loss" and r.status_stoploss == "expired"
    assert r.note == "screening LOSS"
    assert r.api_requests_used == 1


# --------------------------------------------------------------------------
# 11 — neither threshold ever hit -> loss/loss, which_first 'none'
# --------------------------------------------------------------------------
def test_neither_threshold():
    hours = [
        hourly(HOUR_START, 2.5, CH_LO),                      # screening passes on 2.0
        hourly(HOUR_END, 2.5, 0.7),                          # high < 2.6 target, low > 0.65 stop
        hourly(HOUR_END + timedelta(hours=1), 2.4, 0.75),
    ]
    mins = [pp(CALL_TS.replace(second=0), 1.3, 1.4, 1.25)]   # entry 1.3 -> target 2.6, stop 0.65
    r = evaluate(hours, mins)
    # neither 2x nor the 0.65 stop printed -> plain loss; stop strategy
    # EXPIRED (user rule 2026-09), marked at its final close 0.75 (0.58x).
    assert r.status_plain == "loss" and r.status_stoploss == "expired"
    assert r.final_close_usd == 0.75
    assert r.which_threshold_first == "none"
    assert r.target_2x_reached is False and r.minus_50_reached is False
    assert r.api_requests_used == 2
