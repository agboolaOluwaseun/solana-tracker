"""Leaderboard view — ranks channels by win rate + avg peak profit."""
from __future__ import annotations

import streamlit as st

from config import settings
from analysis.leaderboard import leaderboard_frame
from ui.formatting import fmt_pct


def render() -> None:
    st.header("🏆 Channel Leaderboard")
    st.caption(
        f"Channels ranked by overall win rate. A win = peak >= {settings.win_multiplier}x "
        f"within {settings.peak_window_hours}h of the call. Unpriceable calls are excluded from win rate."
    )

    df = leaderboard_frame()
    if df.empty:
        st.info("No data yet. Run a backfill from the sidebar to populate the leaderboard.")
        return

    # --- KPI row -----------------------------------------------------------
    c1, c2, c3 = st.columns(3)
    c1.metric("Channels tracked", len(df))
    c2.metric(
        "Top win rate",
        f"{df['win_rate'].max():.1f}%" if df["win_rate"].notna().any() else "—",
    )
    c3.metric(
        "Total calls scored",
        int(df["total_calls"].sum()),
    )

    # --- Chart -------------------------------------------------------------
    chart_df = df.copy()
    chart_df["label"] = chart_df["channel_title"].fillna(chart_df["channel_username"])
    st.subheader("Win rate by channel")
    st.bar_chart(
        chart_df.set_index("label")["win_rate"],
        width="stretch",
    )

    # --- Table -------------------------------------------------------------
    st.subheader("Rankings")
    display = df.copy()
    display["win_rate"] = display["win_rate"].map(lambda v: fmt_pct(v, decimals=1))
    display["avg_peak_profit_pct"] = display["avg_peak_profit_pct"].map(
        lambda v: fmt_pct(v, sign=True, decimals=0)
    )
    display["best_peak_profit_pct"] = display["best_peak_profit_pct"].map(
        lambda v: fmt_pct(v, sign=True, decimals=0)
    )
    st.dataframe(
        display[
            [
                "rank",
                "channel_title",
                "channel_username",
                "total_calls",
                "wins",
                "win_rate",
                "avg_peak_profit_pct",
                "best_peak_profit_pct",
            ]
        ],
        width="stretch",
        hide_index=True,
    )

    csv = display.to_csv(index=False).encode()
    st.download_button(
        "⬇️ Download leaderboard (CSV)",
        csv,
        file_name="leaderboard.csv",
        mime="text/csv",
    )
