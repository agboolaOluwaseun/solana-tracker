"""
Shared formatting helpers for the Streamlit views.

Handles the None-vs-NaN quirk in pandas 3.0 (where aggregate functions over
all-null columns return Python None instead of float('nan')). The `v == v`
NaN-check idiom doesn't catch None, which previously crashed `.map()`.
"""
from __future__ import annotations

import math


def _is_blank(v) -> bool:
    """True for None or NaN (covers both pandas-3 None and float nan)."""
    if v is None:
        return True
    try:
        return isinstance(v, float) and math.isnan(v)
    except (TypeError, ValueError):
        return False


def fmt_pct(v, sign: bool = False, decimals: int = 1) -> str:
    """Format a fraction-as-percentage. '—' if blank."""
    if _is_blank(v):
        return "—"
    prefix = "+" if sign and v > 0 else ""
    return f"{prefix}{v:.{decimals}f}%"


def fmt_int(v) -> str:
    if _is_blank(v):
        return "—"
    return str(int(v))


def fmt_price(v, decimals: int = 8) -> str:
    """USD price. Trims trailing zeros for cleaner display."""
    if _is_blank(v):
        return "—"
    s = f"{v:.{decimals}f}".rstrip("0").rstrip(".")
    return f"${s}"


def fmt_multiplier(v, decimals: int = 1) -> str:
    """e.g. 3.0 -> '3.0x'."""
    if _is_blank(v):
        return "—"
    return f"{v:.{decimals}f}x"
