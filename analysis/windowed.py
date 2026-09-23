"""
Window-aware analysis for the Channel Deep-Dive display.

Supports display timeframes 1d / 7d / 1m / 3m / all and both strategies
('normal' = 2x peak, 'stoploss' = 2x without a 50% dip). Unpriceable calls are
EXCLUDED from every metric (denominator = win + loss).

Granularity for the bucketed chart:
  1m  -> weekly buckets
  3m / all -> calendar-month buckets
  1d / 7d  -> daily buckets
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from db import get_connection

DISPLAY_WINDOWS = ("1d", "7d", "1m", "3m", "all")


def window_since(window: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """Return the 'since' cutoff for a display window, or None for all-time."""
    if window == "all":
        return None
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    if window == "1d":
        return now - timedelta(days=1)
    if window == "7d":
        return now - timedelta(days=7)
    if window == "1m":
        return _months_ago(now, 1)
    if window == "3m":
        return _months_ago(now, 3)
    raise ValueError(f"unknown display window '{window}'")


def _months_ago(now: datetime, months: int) -> datetime:
    y, m = now.year, now.month
    for _ in range(months):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return datetime(y, m, now.day, now.hour, now.minute, now.second)


def _strategy_join(strategy: str) -> str:
    """SQL join + status filter for a strategy.

    Denominator = decided calls only (win + loss). Unpriceable AND pending
    (incl. immature) are excluded from every metric.
    'trailing' (strategy 3, 50% trailing stop) shares the 'sr' alias with
    stoploss so every downstream sr.<col> reference works unchanged —
    trailing_results carries the same is_win/status/peak_profit_pct columns.
    """
    if strategy == "stoploss":
        return (
            "JOIN stoploss_results sr ON sr.call_id = cal.id "
            "AND sr.status IN ('win','loss')"
        )
    if strategy == "trailing":
        return (
            "JOIN trailing_results sr ON sr.call_id = cal.id "
            "AND sr.status IN ('win','loss')"
        )
    return "AND cal.status IN ('win','loss')"


def _win_col(strategy: str) -> str:
    return "sr.is_win" if strategy in ("stoploss", "trailing") else "cal.is_win"


def _chain_clause(chain: Optional[str]) -> tuple[str, list]:
    """SQL fragment restricting a query to one chain ('sol'|'robinhood'|'eth'|'bsc'|'base'|'arc') or all."""
    if chain in (None, "all"):
        return "", []
    return " AND cal.chain = ?", [chain]


def channel_stats_window(channel_id: int, window: str, strategy: str,
                         chain: Optional[str] = "all") -> Dict:
    """Win rate / counts / avg peak for a channel over a display window."""
    since = window_since(window)
    conn = get_connection()
    win_col = _win_col(strategy)
    chain_sql, chain_params = _chain_clause(chain)
    if strategy in ("stoploss", "trailing"):
        q = f"""
            SELECT COUNT(sr.id) AS total_calls, SUM(sr.is_win) AS wins,
                   AVG(sr.peak_profit_pct) AS avg_peak_profit_pct
            FROM calls cal {_strategy_join(strategy)}
            WHERE cal.channel_id = ?{chain_sql}
        """
    else:
        q = f"""
            SELECT COUNT(cal.id) AS total_calls, SUM(cal.is_win) AS wins,
                   AVG(cal.peak_profit_pct) AS avg_peak_profit_pct
            FROM calls cal
            WHERE cal.channel_id = ?{chain_sql} {_strategy_join(strategy)}
        """
    params: list = [channel_id] + chain_params
    if since:
        q += " AND cal.call_timestamp >= ?"
        params.append(_iso(since))
    row = conn.execute(q, params).fetchone()
    total = row["total_calls"] or 0
    wins = row["wins"] or 0
    return {
        "total_calls": total,
        "wins": wins,
        "win_rate": (wins / total * 100.0) if total else None,
        "avg_peak_profit_pct": row["avg_peak_profit_pct"],
    }


def channel_buckets(channel_id: int, window: str, strategy: str,
                    chain: Optional[str] = "all") -> List[Dict]:
    """Bucketed win rates for the deep-dive chart.

    Granularity: 1m -> weekly; 3m/all -> monthly; 1d/7d -> daily.
    """
    since = window_since(window)
    if window in ("3m", "all"):
        fmt = "%Y-%m"
    elif window == "1m":
        fmt = None  # weekly handled below
    else:
        fmt = "%Y-%m-%d"

    conn = get_connection()
    win_col = _win_col(strategy)
    join = _strategy_join(strategy)
    chain_sql, chain_params = _chain_clause(chain)
    where = "cal.channel_id = ?" + chain_sql
    params: list = [channel_id] + chain_params
    if since:
        where += " AND cal.call_timestamp >= ?"
        params.append(_iso(since))

    if strategy in ("stoploss", "trailing"):
        base = f"FROM calls cal {join} WHERE {where}"
        sel = f"SELECT cal.call_timestamp AS ts, sr.is_win AS w, sr.peak_profit_pct AS p {base}"
    else:
        base = f"FROM calls cal WHERE {where} {join}"
        sel = f"SELECT cal.call_timestamp AS ts, cal.is_win AS w, cal.peak_profit_pct AS p {base}"
    rows = conn.execute(sel, params).fetchall()

    groups: Dict[str, List[int]] = {}
    for r in rows:
        ts = datetime.fromisoformat(r["ts"].replace("Z", ""))
        if fmt is None:  # weekly: ISO week label
            key = f"{ts.isocalendar()[0]}-W{ts.isocalendar()[1]:02d}"
        else:
            key = ts.strftime(fmt)
        groups.setdefault(key, []).append(int(r["w"] or 0))

    out = []
    for key in sorted(groups):
        wins = sum(groups[key])
        total = len(groups[key])
        out.append({
            "bucket": key,
            "total_calls": total,
            "wins": wins,
            "win_rate": (wins / total * 100.0) if total else None,
        })
    return out


def current_streak(channel_id: int, strategy: str,
                   chain: Optional[str] = "all") -> int:
    """Current consecutive-win streak (most recent decided calls, newest first)."""
    conn = get_connection()
    win_col = _win_col(strategy)
    chain_sql, chain_params = _chain_clause(chain)
    if strategy in ("stoploss", "trailing"):
        q = f"""
            SELECT sr.is_win AS w FROM calls cal {_strategy_join(strategy)}
            WHERE cal.channel_id = ?{chain_sql} ORDER BY cal.call_timestamp DESC
        """
    else:
        q = f"""
            SELECT cal.is_win AS w FROM calls cal
            WHERE cal.channel_id = ?{chain_sql} {_strategy_join(strategy)}
            ORDER BY cal.call_timestamp DESC
        """
    rows = conn.execute(q, [channel_id] + chain_params).fetchall()
    streak = 0
    for r in rows:
        if int(r["w"] or 0) == 1:
            streak += 1
        else:
            break
    return streak


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat() + "Z"