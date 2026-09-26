"""Strategy 3 — 50% trailing stop: pure-scorer tests.

Semantic contract (user, 2026-09-23):
  * exit_multiple = fraction of CAPITAL RETURNED. 0.9x exit on a 1.8x
    peak is a LOSS OF 10% (loss_pct = -10), NOT "up 90%".
  * A trailing stop-out can only happen below 2x peak, so every stop-out
    exit lies in [0.5, 1.0) — a real loss, floored at -50%.
  * Same-candle 2x touches = WIN for all strategies (user rule).
"""
from datetime import datetime, timedelta

from models import PricePoint
from pricing.trailing import score_candles_trailing

T0 = datetime(2026, 9, 1, 12, 0)   # naive UTC — documented engine convention


def bar(i, o, h, l, c, minute=False):
    d = timedelta(minutes=1) if minute else timedelta(hours=1)
    return PricePoint(timestamp=T0 + d * i, open=o, high=h, low=l, close=c,
                      volume=1000.0)


def ride(peak, crash_to, tail=3, entry=1.0):
    """entry bar -> climbs to `peak` -> retraces to `crash_to` -> flat tail."""
    bars = [bar(0, entry, entry * 1.05, entry * 0.98, entry * 1.02)]
    steps = 4
    for s in range(1, steps + 1):
        p = entry + (peak - entry) * s / steps
        bars.append(bar(s, p * 0.99, p, p * 0.96, p * 0.98))
    bars.append(bar(steps + 1, peak, peak, crash_to, crash_to))
    for t in range(steps + 2, steps + 2 + tail):
        bars.append(bar(t, crash_to, crash_to * 1.02, crash_to * 0.97, crash_to))
    return bars


# ---- the Bibi examples from the spec, exact ----

def test_peak_1_8x_stops_out_is_0_9x_call_minus_10():
    r = score_candles_trailing(ride(1.8, 0.4), T0)
    assert r.status == "loss" and r.hit_trailing_stop
    assert abs(r.peak_multiple - 1.8) < 1e-9
    assert abs(r.exit_multiple - 0.9) < 1e-9       # capital returned = 90c
    assert abs(r.loss_pct - (-10.0)) < 1e-6         # a LOSS of 10%, not +90


def test_peak_1_3x_stops_out_is_0_65x_call_minus_35():
    r = score_candles_trailing(ride(1.3, 0.3), T0)
    assert r.status == "loss" and r.hit_trailing_stop
    assert abs(r.exit_multiple - 0.65) < 1e-9
    assert abs(r.loss_pct - (-35.0)) < 1e-6


def test_barely_moves_stops_at_half_with_0_52x_call():
    # peak 1.05x: floor = 0.525 of entry, crash below it
    r = score_candles_trailing(ride(1.05, 0.3), T0)
    assert r.status == "loss" and r.hit_trailing_stop
    assert abs(r.exit_multiple - 0.525) < 1e-9
    assert abs(r.loss_pct + 47.5) < 1e-6


# ---- win paths ----

def test_reaches_2x_before_trail_is_win_and_reports_full_peak():
    r = score_candles_trailing(ride(2.4, 0.4), T0)
    assert r.status == "win" and r.is_win and not r.hit_trailing_stop
    assert abs(r.peak_multiple - 2.4) < 1e-9
    assert r.exit_multiple is None                  # win: no stop-out to record


def test_same_candle_spikes_to_2x_and_crashes_is_win():
    # user's standing tie rule: 2x touched in the same bar that breaks the
    # trailing floor => WIN for all strategies.
    bars = [bar(0, 1.0, 1.05, 0.98, 1.0),
            bar(1, 1.2, 2.1, 0.5, 0.6)]
    r = score_candles_trailing(bars, T0)
    assert r.status == "win" and r.is_win


def test_trailing_ejects_a_call_the_fixed_stop_would_hold():
    # 1.6x peak -> dip to 0.7x (above the fixed 0.5 floor, below the
    # trailing 0.8 floor) -> would later recover to 2x. Fixed strategy
    # holds (never <=0.5); trailing stops at 0.8 BEFORE the recovery bar.
    bars = [bar(0, 1.0, 1.05, 0.98, 1.0)]
    for s, p in [(1, 1.2), (2, 1.4), (3, 1.6)]:
        bars.append(bar(s, p * 0.99, p, p * 0.96, p))
    bars.append(bar(4, 1.6, 1.6, 0.7, 0.72))        # dip kills the 0.8 floor
    bars.append(bar(5, 0.72, 2.5, 0.7, 2.4))        # the escape that never counts
    r = score_candles_trailing(bars, T0)
    assert r.status == "loss" and r.hit_trailing_stop
    assert abs(r.exit_multiple - 0.8) < 1e-9


# ---- structure ----

def test_floor_never_falls_below_entry_half():
    # long flat grind (no new highs) then one dump: the floor can never
    # fall below entry/2, so exit is exactly 0.5x.
    bars = [bar(i, 1.0, 1.0, 0.99, 1.0) for i in range(5)]
    bars.append(bar(5, 1.0, 1.0, 0.2, 0.2))
    r = score_candles_trailing(bars, T0)
    assert r.status == "loss" and abs(r.exit_multiple - 0.5) < 1e-9

    # and a tiny grind above entry lifts it: peak 1.02 -> floor 0.51
    bars2 = [bar(i, 1.0, 1.02, 0.99, 1.0) for i in range(5)]
    bars2.append(bar(5, 1.0, 1.0, 0.2, 0.2))
    r2 = score_candles_trailing(bars2, T0)
    assert r2.status == "loss" and abs(r2.exit_multiple - 0.51) < 1e-9


def test_candles_before_call_ignored():
    # huge pre-call pump must NOT create a false peak/floor
    bars = [bar(-5, 1.0, 5.0, 1.0, 5.0)] + ride(1.8, 0.4)
    bars[0] = PricePoint(timestamp=T0 - timedelta(hours=5), open=1, high=5,
                         low=1, close=5, volume=1)
    r = score_candles_trailing(bars, T0)
    assert abs(r.peak_multiple - 1.8) < 1e-9


def test_no_candles_is_unpriceable_not_loss():
    r = score_candles_trailing([], T0)
    assert r.status == "unpriceable_loss" and r.error == "no candles provided"


def test_window_end_without_trigger_is_expired_not_loss():
    # user rule 2026-09: expiry is NOT a verdict for stop strategies.
    # Flat grind that never reaches 2x and never breaks the rising floor.
    bars = [bar(i, 1.0, 1.05, 0.98, 1.02) for i in range(10)]
    bars += [bar(10 + i, 1.02, 1.04, 1.0, 1.01) for i in range(10)]
    r = score_candles_trailing(bars, T0)
    assert r.status == "expired" and not r.hit_trailing_stop and not r.is_win
    # the gray tile still shows its last mark-to-market
    assert r.exit_multiple is not None and abs(r.exit_multiple - 1.01) < 1e-9


def test_wick_clip_defends_a_CALI_style_artifact():
    # entry 1.0; real trade flat ~1.1; ONE minute prints 50x then vanishes.
    # Without clipping it would fake a peak (floor 25x) — but it would also
    # never reach 2x closes so not a win; clipping keeps peak honest at 1.1.
    bars = [bar(i, 1.0, 1.1, 0.95, 1.05) for i in range(10)]
    bars[5] = PricePoint(timestamp=T0 + timedelta(hours=5), open=1.05,
                         high=50.0, low=1.0, close=1.05, volume=3)
    bars[6] = PricePoint(timestamp=T0 + timedelta(hours=6), open=1.04,
                         high=1.1, low=1.0, close=1.04, volume=800)
    r_clip = score_candles_trailing(bars, T0)                    # clipped
    r_raw = score_candles_trailing(bars, T0, wick_clip=False)    # artifact kept
    assert r_raw.peak_multiple >= 40                # artifact visible if unfiltered
    assert r_clip.peak_multiple < 1.6               # filtered verdict is honest


# ---- entry anchor: engine's stored dollar beats re-derived hour open ----

def test_entry_override_anchors_verdict_to_engine_dollar():
    # hour opens CHEAP ($1.91), true call-minute entry $2.91. Post-entry
    # bars dip to $1.2: below the engine floor (1.455) but ABOVE the
    # hour-open floor (0.955->1.0). Only the honest anchor sees the stop.
    bars = [bar(0, 1.91, 2.0, 1.5, 1.6)] + \
           [bar(i, 1.5, 1.55, 1.2, 1.3) for i in range(1, 8)]
    plain = score_candles_trailing(bars, T0 + timedelta(hours=2))
    # (call 2h in: bars 1..7 are post-call; bar0 is historical anyway)
    anchored = score_candles_trailing(bars, T0, entry_override=2.91)
    assert anchored.entry_price_usd == 2.91                   # canonical dollar
    assert anchored.status == "loss" and anchored.hit_trailing_stop
    assert abs(anchored.exit_multiple - 1.455 / 2.91) < 1e-9  # honest ~0.5x call
    # hour-open anchor: same bars, its floor (1.0 from peak 2.0) never sees
    # the 1.2 low -> no stop-out. Anchor choice alone flips the verdict.
    rederived = score_candles_trailing(bars, T0)
    assert rederived.entry_price_usd == 1.91
    assert not rederived.hit_trailing_stop


def test_entry_bar_pre_call_low_neutralized_but_high_counts():
    # entry bar OPENED before the call with a brutal pre-call low ($0.1) —
    # re-anchored to $2.91 its floor is 1.455; the pre-call low must NOT
    # fabricate a stop-out. The bar's post-call high (3.0) does update peak.
    call = T0 + timedelta(minutes=45)
    bars = [PricePoint(timestamp=T0, open=1.91, high=3.0, low=0.1,
                       close=2.9, volume=10)] + \
           [bar(i, 2.9, 3.0, 2.8, 2.9) for i in range(1, 6)]
    r = score_candles_trailing(bars, call, entry_override=2.91)
    assert not r.hit_trailing_stop                            # no fake stop
    assert r.status == "expired"                              # survives window
    assert r.peak_multiple >= 3.0 / 2.91 - 1e-9               # high still used


def test_entry_override_rejects_flipped_curve():
    # stored engine entry $2.91e-6 but cached curve is the $750 META asset
    bars = [bar(0, 750.0, 755.0, 744.0, 750.0) for _ in range(3)]
    for i in range(3):
        pass
    bars = [bar(i, 750.0, 755.0, 744.0, 750.0) for i in range(3)]
    r = score_candles_trailing(bars, T0, entry_override=2.91e-6)
    assert r.status == "unpriceable_loss"
    assert "entry/curve mismatch" in (r.error or "")


def test_no_override_keeps_old_behavior():
    bars = [bar(0, 1.0, 1.05, 0.98, 1.0), bar(1, 1.0, 1.05, 0.9, 0.95)]
    r = score_candles_trailing(bars, T0)
    assert r.entry_price_usd == 1.0
