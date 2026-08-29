"""
Strategy-2 ("-50%") analysis: wins when the price reaches 2x the call price
without first dropping to 50% of it. Reads from stoploss_results (populated
during the normal pricing pass), joined to calls/channels.

Unpriceable calls are EXCLUDED from every metric — win rate denominator is
only priced calls (win + loss), matching the normal strategy.
"""
from __future__ import annotations

from typing import Dict

import pandas as pd

from db import get_connection


def stoploss_leaderboard_frame() -> pd.DataFrame:
    """
    Rank channels by strategy-2 overall win rate.

    Unpriceable rows are excluded (the join filters sr.status !=
    'unpriceable_loss'). Only channels with at least one scored strategy-2
    call appear. Win is a win (is_win=1); a stop-out or never-reached-2x is a
    loss. Sorted by win_rate desc, avg peak desc, total calls desc.
    """
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT c.id            AS channel_id,
               c.title         AS channel_title,
               c.username      AS channel_username,
               COUNT(sr.id)    AS total_calls,
               SUM(sr.is_win)  AS wins,
               AVG(sr.peak_profit_pct) AS avg_peak_profit_pct,
               MAX(sr.peak_profit_pct) AS best_peak_profit_pct
        FROM channels c
        LEFT JOIN calls cal ON cal.channel_id = c.id
        LEFT JOIN stoploss_results sr ON sr.call_id = cal.id
            AND sr.status NOT IN ('unpriceable_loss', 'excluded')
        GROUP BY c.id
        HAVING COUNT(sr.id) > 0
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
    df = df.sort_values(
        by=["win_rate", "avg_peak_profit_pct", "total_calls"],
        ascending=[False, False, False],
        na_position="last",
    ).reset_index(drop=True)
    df.insert(0, "rank", df.index + 1)
    return df


def stoploss_channel_detail(channel_id: int) -> dict:
    """Compact one-channel summary for the strategy-2 deep-dive header."""
    conn = get_connection()
    row = conn.execute(
        """
        SELECT c.title, c.username, c.window_start, c.window_end,
               COUNT(sr.id)   AS total_calls,
               SUM(sr.is_win) AS wins,
               AVG(sr.peak_profit_pct) AS avg_peak_profit_pct,
               MAX(sr.peak_profit_pct) AS best_peak_profit_pct
        FROM channels c
        LEFT JOIN calls cal ON cal.channel_id = c.id
        LEFT JOIN stoploss_results sr ON sr.call_id = cal.id
            AND sr.status NOT IN ('unpriceable_loss', 'excluded')
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


def load_stoploss_calls_frame(channel_id: int | None = None) -> pd.DataFrame:
    """Load strategy-2 rows (optionally one channel); unpriceable excluded."""
    conn = get_connection()
    if channel_id is None:
        rows = conn.execute(
            """
            SELECT cal.channel_id, cal.week_index, cal.token_symbol,
                   cal.token_name, cal.token_address, cal.call_timestamp,
                   sr.entry_price_usd, sr.peak_price_usd, sr.peak_timestamp,
                   sr.peak_profit_pct, sr.is_win, sr.status, sr.hit_stoploss,
                   sr.stoploss_timestamp
            FROM calls cal
            JOIN stoploss_results sr ON sr.call_id = cal.id
            WHERE sr.status != 'unpriceable_loss'
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT cal.channel_id, cal.week_index, cal.token_symbol,
                   cal.token_name, cal.token_address, cal.call_timestamp,
                   sr.entry_price_usd, sr.peak_price_usd, sr.peak_timestamp,
                   sr.peak_profit_pct, sr.is_win, sr.status, sr.hit_stoploss,
                   sr.stoploss_timestamp
            FROM calls cal
            JOIN stoploss_results sr ON sr.call_id = cal.id
            WHERE cal.channel_id = ? AND sr.status NOT IN ('unpriceable_loss', 'excluded')
            """,
            (channel_id,),
        ).fetchall()
    df = pd.DataFrame(rows)
    if not df.empty:
        df["call_timestamp"] = pd.to_datetime(df["call_timestamp"], errors="coerce", utc=True)
        df["peak_timestamp"] = pd.to_datetime(df["peak_timestamp"], errors="coerce", utc=True)
        df["stoploss_timestamp"] = pd.to_datetime(df["stoploss_timestamp"], errors="coerce", utc=True)
    return df


def stoploss_weekly(channel_id: int) -> pd.DataFrame:
    """Strategy-2 win rate per week (1..window_weeks) for a channel."""
    from config import settings

    df = load_stoploss_calls_frame(channel_id)
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

    full = pd.DataFrame({"week_index": list(range(1, window_weeks + 1))})
    grouped = full.merge(grouped, on="week_index", how="left")
    grouped["total_calls"] = grouped["total_calls"].fillna(0).astype(int)
    grouped["wins"] = grouped["wins"].fillna(0).astype(int)
    return grouped


def stoploss_overall(channel_id: int) -> Dict[str, float]:
    """Strategy-2 overall win rate + counts for a channel."""
    df = load_stoploss_calls_frame(channel_id)
    if df.empty:
        return {"total_calls": 0, "wins": 0, "win_rate": float("nan")}
    wins = int(df["is_win"].sum())
    total = int(len(df))
    return {
        "total_calls": total,
        "wins": wins,
        "win_rate": (wins / total * 100.0) if total else float("nan"),
    }