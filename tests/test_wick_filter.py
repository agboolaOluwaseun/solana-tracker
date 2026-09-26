"""Fake-HIGH wick filter (user policy 2026-09-24: museic + CALI lessons).

Contract under test:
  hour gate 3.0 x close  -> only suspicious HOURS get a minute look
  minute gate 1.3 x close -> a fake minute's high := mean of its two
                           nearest existing neighbours' highs
  edge: first of series -> mean(+1,+2); last -> mean(-1,-2)
  no minute trips       -> the hour is a GENUINE pump&dump: keep whole
  minutes unavailable   -> hourly-neighbour repair, never raises
  lows are NEVER touched (flash lows must stand — user's stop logic)
"""
from datetime import datetime, timedelta

from models import PricePoint
from pricing.wick_filter import repair_hourly, repair_minutes

T0 = datetime(2026, 9, 22, 15, 0)


def m(i, o, h, l, c):
    return PricePoint(timestamp=T0 + timedelta(minutes=i), open=o, high=h,
                      low=l, close=c, volume=10.0)


def h(i, o, hi, lo, c):
    return PricePoint(timestamp=T0 + timedelta(hours=i), open=o, high=hi,
                      low=lo, close=c, volume=10.0)


def no_minutes(start, end):
    return [], 0


# ── minute level ───────────────────────────────────────────────────────────

def test_museic_2039_minute_repairs_to_neighbour_mean():
    # real numbers: 20:38 h≈4.03e-6, 20:39 h 2.002e-5 c 6.26e-6, 20:40 h 6.26e-6
    mins = [m(0, 4.0e-6, 4.05e-6, 4.0e-6, 4.03e-6),
            m(1, 4.03e-6, 2.002e-5, 4.03e-6, 6.26e-6),      # wick 3.20x
            m(2, 6.26e-6, 6.26e-6, 5.69e-6, 5.69e-6),
            m(3, 5.69e-6, 6.32e-6, 5.69e-6, 6.32e-6)]
    out, n = repair_minutes(mins)
    assert n == 1
    assert abs(out[1].high - (4.05e-6 + 6.26e-6) / 2) < 1e-12
    # close/open/low untouched; other minutes untouched
    assert out[1].close == 6.26e-6 and out[1].open == 4.03e-6
    assert out[2].high == 6.26e-6 and out[0].high == 4.05e-6


def test_genuine_minute_staircase_survives():
    # MOTION-style sustained climb: high ≈ close on every bar
    mins = [m(i, 1.0 + 0.1 * i, 1.02 + 0.1 * i, 0.98 + 0.1 * i, 1.0 + 0.1 * i)
            for i in range(8)]
    out, n = repair_minutes(mins)
    assert n == 0
    assert [p.high for p in out] == [p.high for p in mins]


def test_first_minute_of_series_uses_plus_one_plus_two():
    # neighbours 3.0 and 3.5 keep their own ratios ~1.03 (no accidental trips)
    mins = [m(0, 1.0, 10.0, 1.0, 2.0),          # 5x wick at the very start
            m(1, 2.0, 3.0, 2.0, 2.9),           # 3.0/2.9 = 1.03 clean
            m(2, 2.9, 3.5, 2.9, 3.4),           # 3.5/3.4 = 1.03 clean
            m(3, 3.4, 3.6, 3.4, 3.5)]
    out, n = repair_minutes(mins)
    assert n == 1 and abs(out[0].high - (3.0 + 3.5) / 2) < 1e-12


def test_last_minute_of_series_uses_minus_one_minus_two():
    mins = [m(0, 2.0, 3.0, 2.0, 2.9),
            m(1, 2.9, 4.0, 2.9, 3.9),
            m(2, 3.9, 20.0, 3.9, 4.0)]          # 5x wick at the very end
    out, n = repair_minutes(mins)
    assert n == 1 and abs(out[2].high - (3.0 + 4.0) / 2) < 1e-12


def test_missing_immediate_neighbour_skips_to_next_existing():
    # series starts at minute idx 1 (minute 0 traded nothing): the wick at
    # idx 1 has only ONE left neighbour -> nearest-two rule uses right side
    mins = [m(1, 1.0, 1.05, 1.0, 1.02),         # ratio 1.03 clean
            m(2, 1.02, 9.0, 1.02, 1.10),        # wick 8.2x
            m(3, 1.10, 1.2, 1.10, 1.15),        # clean
            m(4, 1.15, 1.25, 1.15, 1.20)]       # clean
    out, n = repair_minutes(mins)
    assert n == 1
    # left side has just idx1 (1.05); right nearest idx3 (1.2) -> nearest two
    assert abs(out[1].high - (1.05 + 1.2) / 2) < 1e-12


def test_lows_are_never_touched():
    # flash DUMP minute: low 10x below close -> must survive verbatim
    mins = [m(0, 1.0, 1.1, 0.9, 1.0),
            m(1, 1.0, 1.05, 0.09, 1.0),         # violent single-print low
            m(2, 1.0, 1.1, 0.95, 1.0)]
    out, n = repair_minutes(mins)
    assert n == 0 and out[1].low == 0.09


def test_two_culprits_in_one_hour_both_repaid():
    mins = [m(0, 1.0, 1.1, 1.0, 1.05),
            m(1, 1.05, 6.0, 1.05, 1.06),        # wick
            m(2, 1.06, 1.1, 1.06, 1.07),
            m(3, 1.07, 5.0, 1.07, 1.08),        # wick
            m(4, 1.08, 1.12, 1.08, 1.1)]
    out, n = repair_minutes(mins)
    assert n == 2 and out[1].high < 6.0 and out[3].high < 5.0


# ── hourly orchestration ───────────────────────────────────────────────────

def test_healthy_hour_zero_minute_calls():
    calls = {"n": 0}

    def get_m(s, e):
        calls["n"] += 1
        return [], 0
    series = [h(0, 1.0, 1.2, 0.9, 1.1), h(1, 1.1, 1.3, 1.0, 1.2)]
    out, reqs = repair_hourly(series, get_m)
    assert reqs == 0 and calls["n"] == 0        # gate: high/close <= 3
    assert [p.high for p in out] == [1.2, 1.3]


def test_suspect_hour_innocent_at_minute_level_is_kept_whole():
    # intra-hour pump&dump: hour high 3.2x close BUT the peak held for
    # many minutes — minute scan finds no >1.3x prints -> keep hour.
    series = [h(0, 1.0, 3.2, 1.0, 1.05), h(1, 1.05, 1.2, 1.0, 1.1)]
    mins = [m(i, 1 + 0.05 * i, 1 + 0.05 * i + 0.02, 1 + 0.05 * i - 0.01,
              1 + 0.05 * i + 0.015) for i in range(64)]

    def get_m(s, e):
        return [x for x in mins if s <= x.timestamp < e], 1
    out, reqs = repair_hourly(series, get_m)
    assert reqs == 1                            # one drill-down paid
    assert out[0].high == 3.2                   # hour kept WHOLE
    assert out[0].close == 1.05


def test_suspect_hour_with_real_wicks_gets_rebuilt_hour():
    # the museic shape: hour 16:00 high 2.002e-5 from ONE wicky minute
    # (16:45); the rest of the hour sustains ~6.5e-6. Rebuilt hour keeps
    # the sustained top and loses the wick.
    entry_hour = h(0, 2.9e-6, 2.92e-6, 2.5e-6, 2.91e-6)      # ratio 1.0 clean
    spike_hour = h(1, 2.91e-6, 2.002e-5, 2.5e-6, 4.31e-6)    # 4.65x -> suspect
    after = h(2, 4.31e-6, 5.6e-6, 4.0e-6, 5.0e-6)            # ratio 1.12 clean
    base = T0 + timedelta(hours=1) - timedelta(minutes=3)    # fetch covers
    mins = []                                                # 15:57..17:02
    for i in range(66):
        sustained = 6.5e-6 if 16 <= i <= 60 else 3.0e-6
        hi = sustained * 6.0 if i == 48 else sustained * 1.02
        mins.append(PricePoint(timestamp=base + timedelta(minutes=i),
                               open=sustained, high=hi,
                               low=sustained * 0.95, close=sustained * 1.01,
                               volume=5))

    def get_m(s, e):
        return [x for x in mins if s <= x.timestamp < e], 1
    out, reqs = repair_hourly([entry_hour, spike_hour, after], get_m)
    assert reqs >= 1
    fixed = out[1]
    assert fixed.high < 2.002e-5                  # wick gone
    assert 6.5e-6 <= fixed.high <= 6.7e-6         # sustained top kept (≈6.63e-6)
    assert fixed.low <= 3.0e-6 * 0.96             # lows stand untouched


def test_minutes_unfetchable_uses_hourly_neighbour_fallback():
    series = [h(0, 1.0, 1.5, 1.0, 1.2),
              h(1, 1.2, 20.0, 1.1, 1.5),        # 13x high/close, minutes dead
              h(2, 1.5, 1.8, 1.4, 1.6)]
    out, reqs = repair_hourly(series, no_minutes)
    assert out[1].high == (1.5 + 1.8) / 2       # neighbour mean
    assert out[1].high < 20.0
    # lows/closes untouched
    assert out[1].low == 1.1 and out[1].close == 1.5


def test_fallback_never_raises_a_high():
    # neighbours HIGHER than the suspect's own high -> leave it alone
    series = [h(0, 1.0, 9.0, 1.0, 1.5),
              h(1, 1.5, 5.0, 1.4, 1.45),        # 3.45x high/close -> suspect
              h(2, 1.45, 9.5, 1.4, 1.5)]
    out, _ = repair_hourly(series, no_minutes)
    assert out[1].high == 5.0                   # min(neighbour_mean, own)


def test_first_and_last_hour_one_sided_fallback():
    series = [h(0, 1.0, 30.0, 1.0, 2.0),        # first hour, wicky, no mins
              h(1, 2.0, 4.0, 2.0, 3.0),
              h(2, 3.0, 5.0, 3.0, 4.0)]
    out, _ = repair_hourly(series, no_minutes)
    assert out[0].high == (4.0 + 2.0) / 2       # mean(next high, own close)


def test_lows_survive_full_hourly_path():
    series = [h(0, 1.0, 1.2, 1.0, 1.05),
              h(1, 1.05, 4.0, 0.01, 1.1),       # suspect high AND a flash low
              h(2, 1.1, 1.3, 1.0, 1.2)]
    out, _ = repair_hourly(series, no_minutes)
    assert out[1].low == 0.01                   # low untouched — stops respect it


def test_filter_disabled_by_config(monkeypatch):
    import config
    import dataclasses
    monkeypatch.setattr(config, "settings",
                        dataclasses.replace(config.settings, wick_filter=False))
    series = [h(0, 1.0, 1.5, 1.0, 1.2), h(1, 1.2, 99.0, 1.1, 1.5),
              h(2, 1.5, 1.8, 1.4, 1.6)]
    out, reqs = repair_hourly(series, no_minutes)
    assert reqs == 0 and out[1].high == 99.0    # off = raw series, no repairs


def test_real_museic_hour_repair_produces_sustained_237x():
    """The exact museic hour-20:00 minutes we audited live; the repaired
    hour's high must match the hand-computed 6.900e-6 (SpyDefi x2.2 class).
    Series is SPARSE (only trading minutes) — neighbour-skip handles it."""
    h20 = datetime(2026, 9, 22, 20, 0)
    hour = PricePoint(timestamp=h20, open=2.919e-06, high=2.002e-05,
                      low=2.534e-06, close=4.306e-06, volume=10)
    rows = [   # (minute, open, high, low, close) abridged from price_cache
        (39, 4.033e-06, 2.002e-05, 4.033e-06, 6.259e-06),   # the wick 3.2x
        (40, 6.259e-06, 6.259e-06, 5.688e-06, 5.692e-06),   # 1.099 clean
        (44, 5.730e-06, 6.325e-06, 5.730e-06, 6.325e-06),
        (45, 6.325e-06, 6.900e-06, 6.325e-06, 6.900e-06),   # real top
        (46, 6.900e-06, 6.900e-06, 5.460e-06, 5.460e-06),   # 1.264 clean
        (49, 5.172e-06, 6.041e-06, 5.172e-06, 6.041e-06),
        (53, 6.009e-06, 6.009e-06, 4.721e-06, 4.782e-06),   # 1.256 clean
    ]
    mins = [PricePoint(timestamp=h20 + timedelta(minutes=i),
                       open=o, high=hh, low=l, close=c, volume=5)
            for i, o, hh, l, c in rows]

    def get_m(s, e):
        return [p for p in mins if s <= p.timestamp < e], 1
    out, _ = repair_hourly([hour], get_m)
    repaired = out[0]
    assert repaired.high < 2.002e-05                 # wick clipped
    assert abs(repaired.high - 6.900e-06) < 1e-09    # = real sustained top
    peak_mult = repaired.high / 2.90994664185432e-06
    assert 2.0 < peak_mult < 3.0                     # 2.37x — SpyDefi's x2.2
