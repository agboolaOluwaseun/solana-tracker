"""
Central configuration for solana_tracker.

Loads from environment (and a local .env file). A single Settings instance is
exposed as `settings` and imported throughout the app.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Project root = the directory this file lives in.
PROJECT_ROOT = Path(__file__).resolve().parent

# Load .env if present (silently ignored otherwise).
load_dotenv(PROJECT_ROOT / ".env")


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


@dataclass(frozen=True)
class Settings:
    # Telegram
    telegram_api_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_API_ID", ""))
    telegram_api_hash: str = field(default_factory=lambda: os.getenv("TELEGRAM_API_HASH", ""))
    session_name: str = field(default_factory=lambda: os.getenv("SESSION_NAME", "solana_tracker"))
    telegram_phone: str = field(default_factory=lambda: os.getenv("TELEGRAM_PHONE", ""))

    # GeckoTerminal
    geckoterminal_api_key: str = field(
        default_factory=lambda: os.getenv("GECKOTERMINAL_API_KEY", "")
    )
    geckoterminal_base_url: str = "https://api.geckoterminal.com/api/v2"
    # GeckoTerminal Solana network id.
    solana_network: str = "solana"

    # Birdeye (alternative pricing source)
    birdeye_api_key: str = field(default_factory=lambda: os.getenv("BIRDEYE_API_KEY", ""))

    # Rate limits (requests per minute). Empirically (2026-08) GeckoTerminal's
    # "Cloudflare protection" is a rolling-window origin limiter that trips at
    # ~23 effective RPM — ABOVE the ~20 advertised. The sustained target is
    # computed by the RateLimiter as RPM * safety_margin (default 0.65), so
    # these are the ADVERTISED ceilings, not the sustained rate.
    free_rpm: int = 20
    paid_rpm: int = 250
    # Fraction of the advertised ceiling we target as a sustained rate. Stays
    # ~35% below the ceiling to avoid tripping Cloudflare during normal runs.
    rate_limit_safety_margin: float = 0.65

    # Database
    db_path: Path = field(
        default_factory=lambda: PROJECT_ROOT / os.getenv("DB_PATH", "solana_tracker.db")
    )

    # Backfill / scoring defaults
    peak_window_hours: int = field(default_factory=lambda: _get_int("PEAK_WINDOW_HOURS", 12))  # 12h (most Solana shitcoins die within 12h)
    win_multiplier: float = field(default_factory=lambda: _get_float("WIN_MULTIPLIER", 2.0))
    window_weeks: int = field(default_factory=lambda: _get_int("WINDOW_WEEKS", 12))
    # OHLCV candle timeframe: "minute" gives the most accurate entry price at the
    # exact call minute (memecoins move 5x+ within a single minute).
    ohlcv_timeframe: str = field(default_factory=lambda: os.getenv("OHLCV_TIMEFRAME", "minute"))

    @property
    def requests_per_minute(self) -> int:
        """Effective GeckoTerminal RPM based on whether a key is present."""
        return self.paid_rpm if self.geckoterminal_api_key else self.free_rpm

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_api_id and self.telegram_api_hash)


settings = Settings()
