"""
Dataclasses used across ingestion, pricing, analysis and UI layers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


@dataclass
class RawMessage:
    """A historical Telegram message that may contain one or more token calls."""
    channel_id: int
    message_id: int
    text: str
    timestamp: datetime                       # UTC


@dataclass
class ParsedCall:
    """A detected call extracted from a message (channel-only model)."""
    channel_id: int
    message_id: int
    raw_text: str
    token_address: str
    token_symbol: Optional[str] = None
    token_name: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class PricePoint:
    """A single OHLCV candle."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class BacktestResult:
    """The outcome of pricing one call over its peak window."""
    entry_price_usd: Optional[float]
    peak_price_usd: Optional[float]
    peak_timestamp: Optional[datetime]
    peak_profit_pct: Optional[float]          # (peak/entry - 1) * 100
    is_win: bool
    status: str                               # win|loss|unpriceable_loss
    pool_address: Optional[str] = None
    candles_used: int = 0
    error: Optional[str] = None


@dataclass
class StoplossResult:
    """The outcome of pricing one call using the 50% stop-loss strategy."""
    entry_price_usd: Optional[float]
    peak_price_usd: Optional[float]         # Max price reached BEFORE stop-loss (if loss) or overall (if win)
    peak_timestamp: Optional[datetime]
    peak_profit_pct: Optional[float]        # (peak/entry - 1) * 100
    hit_stoploss: bool                      # True if price dropped 50% before hitting 2x
    stoploss_timestamp: Optional[datetime]  # When the 50% drop occurred
    is_win: bool                            # True if hit 2x before 50% drop
    status: str                             # win|loss|unpriceable_loss
    pool_address: Optional[str] = None
    candles_used: int = 0
    error: Optional[str] = None


@dataclass
class TrailingResult:
    """Strategy 3 — 50% TRAILING stop. Stop floor = half the RUNNING PEAK
    (never falls). WIN = 2x entry touched before the trailing stop triggers;
    LOSS = a candle low fell to half the peak formed by EARLIER candles.
    A candle that both prints a fresh high and breaks peak/2 is the same-
    candle tie — WIN for all strategies (user rule), same as the 7d engine.
    For a loss: exit_multiple = peak/2 relative to entry = FRACTION OF
    CAPITAL RETURNED (peak<2x in every loss -> exit in [0.5, 1.0), always a
    real loss); loss_pct = (exit_multiple - 1) * 100 (negative)."""
    entry_price_usd: Optional[float]
    peak_multiple: Optional[float]          # running peak / entry (whole window on win)
    peak_price_usd: Optional[float]
    peak_timestamp: Optional[datetime]
    exit_multiple: Optional[float]          # peak/2 on loss (capital returned); None on win
    exit_price_usd: Optional[float]
    exit_timestamp: Optional[datetime]      # candle that triggered the stop (hour precision)
    loss_pct: Optional[float]               # (exit_multiple-1)*100 on loss; None on win
    hit_trailing_stop: bool
    is_win: bool
    status: str                             # win|loss|unpriceable_loss|pending
    pool_address: Optional[str] = None
    candles_used: int = 0
    error: Optional[str] = None


@dataclass
class ChannelStats:
    """Aggregate metrics for one channel over the window."""
    channel_id: int
    channel_title: str
    channel_username: Optional[str]
    total_calls: int
    wins: int
    win_rate: float                           # 0..100
    avg_peak_profit_pct: Optional[float]
    weekly_win_rates: dict    = field(default_factory=dict)  # week_index -> win_rate


@dataclass
class WeekBuckets:
    """The 12 fixed weekly buckets derived from a window start date."""
    start: datetime                           # inclusive
    end: datetime                             # exclusive (start + window_weeks*7d)
    week_starts: list                         # list[datetime] of length window_weeks

    def week_index(self, ts: datetime) -> int:
        """Return 1-based week index for a timestamp, or 0 if outside the window."""
        if ts < self.start or ts >= self.end:
            return 0
        delta_days = (ts - self.start).days
        return (delta_days // 7) + 1
