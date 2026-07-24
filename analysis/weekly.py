"""
Weekly aggregation.

Computes per-week and per-channel win rates over the 12 fixed weekly buckets.
A call is a WIN iff status='win' (peak >= WIN_MULTIPLIER * entry). Unpriceable
calls are EXCLUDED from win rate — the denominator is only priced calls
(win + loss) in the channel for that week.
"""
from __future__ import annotations

from typing import Dict, List

import pandas as pd

from db import get_connection


def load_calls_frame(channel_id: int | None = None) -> pd.DataFrame:
    """Load all calls (optionally for one channel) into a DataFrame.

    Unpriceable calls are EXCLUDED — they don't count in win rate.
    """
    conn = get_connection()
    if channel_id is None:
        rows = conn.execute(
            "SELECT * FROM calls WHERE status != 'unpriceable_loss'"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM calls WHERE channel_id = ? AND status != 'unpriceable_loss'",
            (channel_id,),
        ).fetchall()
    df = pd.DataFrame(rows)
    if not df.empty:
        df["call_timestamp"] = pd.to_datetime(df["call_timestamp"], errors="coerce", utc=True)
        df["peak_timestamp"] = pd.to_datetime(df["peak_timestamp"], errors="coerce", utc=True)
    return df


def weekly_win_rates(channel_id: int) -> pd.DataFrame:
    """
    Win rate per week (1..window_weeks) for a channel.

    Returns a DataFrame with columns:
        week_index, total_calls, wins, win_rate
    Weeks with no calls still appear (win_rate = NaN).
    """
    from config import settings

    df = load_calls_frame(channel_id)
    window_weeks = settings.window_weeks

    if df.empty:
        return pd.DataFrame(
            {"week_index": list(range(1, window_weeks + 1)),
             "total_calls": [0] * window_weeks,
             "wins": [0] * window_weeks,
             "win_rate": [float("nan")] * window_weeks}
        )

    df["is_win_int"] = df["is_win"].astype(int)
    grouped = (
        df.groupby("week_index")["is_win_int"]
        .agg(total_calls="size", wins="sum")
        .reset_index()
    )
    grouped["win_rate"] = grouped["wins"] / grouped["total_calls"] * 100.0

    # Fill in any missing weeks so the chart always spans 1..window_weeks.
    full = pd.DataFrame({"week_index": list(range(1, window_weeks + 1))})
    grouped = full.merge(grouped, on="week_index", how="left")
    grouped["total_calls"] = grouped["total_calls"].fillna(0).astype(int)
    grouped["wins"] = grouped["wins"].fillna(0).astype(int)
    return grouped


def overall_win_rate(channel_id: int) -> Dict[str, float]:
    """Overall win rate + counts for a channel."""
    df = load_calls_frame(channel_id)
    if df.empty:
        return {"total_calls": 0, "wins": 0, "win_rate": float("nan")}
    wins = int(df["is_win"].sum())
    total = int(len(df))
    return {
        "total_calls": total,
        "wins": wins,
        "win_rate": (wins / total * 100.0) if total else float("nan"),
    }
