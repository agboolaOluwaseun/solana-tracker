"""
Leaderboard computation.

Ranks channels by overall win rate, with average peak profit % and total call
count as secondary metrics. Ties are broken by avg peak profit % (higher first),
then by total calls (more first).
"""
from __future__ import annotations

import pandas as pd

from db import get_connection


def leaderboard_frame() -> pd.DataFrame:
    """
    Build the channel leaderboard.

    Unpriceable calls are EXCLUDED from all calculations (total_calls,
    wins, win_rate, avg/best peak profit). Only priced calls (win + loss)
    are counted.

    Returns a DataFrame sorted by win_rate desc, avg_peak_profit_pct desc,
    total_calls desc, with columns:
        rank, channel_id, channel_title, channel_username, total_calls,
        wins, win_rate, avg_peak_profit_pct, best_peak_profit_pct
    """
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT c.id            AS channel_id,
               c.title         AS channel_title,
               c.username      AS channel_username,
               COUNT(cal.id)   AS total_calls,
               SUM(cal.is_win) AS wins,
               AVG(cal.peak_profit_pct) AS avg_peak_profit_pct,
               MAX(cal.peak_profit_pct) AS best_peak_profit_pct
        FROM channels c
        LEFT JOIN calls cal ON cal.channel_id = c.id
            AND cal.status NOT IN ('unpriceable_loss', 'excluded')
        GROUP BY c.id
        """
    ).fetchall()
    df = pd.DataFrame(rows)

    if df.empty:
        return df

    df["total_calls"] = df["total_calls"].fillna(0).astype(int)
    df["wins"] = df["wins"].fillna(0).astype(int)
    df["win_rate"] = df.apply(
        lambda r: (r["wins"] / r["total_calls"] * 100.0) if r["total_calls"] else float("nan"),
        axis=1,
    )
    # Sort: win rate desc, avg peak desc, total calls desc.
    df = df.sort_values(
        by=["win_rate", "avg_peak_profit_pct", "total_calls"],
        ascending=[False, False, False],
        na_position="last",
    ).reset_index(drop=True)
    df.insert(0, "rank", df.index + 1)
    return df


def channel_detail(channel_id: int) -> dict:
    """Compact summary of one channel for the deep-dive header."""
    conn = get_connection()
    row = conn.execute(
        """
        SELECT c.title, c.username, c.window_start, c.window_end,
               COUNT(cal.id) AS total_calls,
               SUM(cal.is_win) AS wins,
               AVG(cal.peak_profit_pct) AS avg_peak_profit_pct,
               MAX(cal.peak_profit_pct) AS best_peak_profit_pct
        FROM channels c
        LEFT JOIN calls cal ON cal.channel_id = c.id
            AND cal.status NOT IN ('unpriceable_loss', 'excluded')
        WHERE c.id = ?
        GROUP BY c.id
        """,
        (channel_id,),
    ).fetchone()
    if not row:
        return {}
    total = row["total_calls"] or 0
    wins = row["wins"] or 0
    return {
        "channel_id": channel_id,
        "title": row["title"] or row["username"] or f"Channel {channel_id}",
        "username": row["username"],
        "window_start": row["window_start"],
        "window_end": row["window_end"],
        "total_calls": total,
        "wins": wins,
        "win_rate": (wins / total * 100.0) if total else None,
        "avg_peak_profit_pct": row["avg_peak_profit_pct"],
        "best_peak_profit_pct": row["best_peak_profit_pct"],
    }
