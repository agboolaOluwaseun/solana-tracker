"""Channel deep-dive view — Week 1..N success rates for a selected channel."""
from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from analysis.leaderboard import channel_detail
from analysis.weekly import overall_win_rate, weekly_win_rates
from db import get_connection
from models import WeekBuckets
from ui.formatting import fmt_pct
from datetime import timedelta
from config import settings


def _channel_options():
    """Return list of (label, channel_id) for the picker."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, title, username FROM channels ORDER BY title"
    ).fetchall()
    opts = []
    for r in rows:
        label = r["title"] or r["username"] or f"Channel {r['id']}"
        if r["username"]:
            label = f"{label} (@{r['username']})"
        opts.append((label, r["id"]))
    return opts


def render() -> None:
    st.header("🔎 Channel Deep-Dive")

    opts = _channel_options()
    if not opts:
        st.info("No channels yet. Run a backfill from the sidebar first.")
        return

    label, channel_id = st.selectbox(
        "Select a channel", opts, format_func=lambda x: x[0], key="deepdive_pick"
    )

    detail = channel_detail(channel_id)
    if not detail:
        st.warning("No data for this channel.")
        return

    # --- Header KPIs -------------------------------------------------------
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total calls", detail["total_calls"])
    c2.metric("Wins", detail["wins"])
    wr = detail["win_rate"]
    c3.metric("Overall win rate", fmt_pct(wr, decimals=1))
    avg = detail["avg_peak_profit_pct"]
    c4.metric("Avg peak profit", fmt_pct(avg, sign=True, decimals=0))

    st.caption(
        f"Window: {detail.get('window_start','?')} → {detail.get('window_end','?')}"
    )

    # --- Weekly chart ------------------------------------------------------
    weekly = weekly_win_rates(channel_id)
    st.subheader(f"Weekly success rate (Week 1–{settings.window_weeks})")

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=[f"W{int(w)}" for w in weekly["week_index"]],
            y=weekly["win_rate"],
            text=weekly["total_calls"].map(
                lambda n: f"{n} calls" if n else "—"
            ),
            textposition="outside",
            marker_color="#00C19F",
            name="Win rate",
        )
    )
    fig.add_hline(
        y=wr if wr is not None else 0,
        line_dash="dash",
        line_color="#FFA500",
        annotation_text=f"overall {wr:.1f}%" if wr is not None else "",
    )
    fig.update_layout(
        yaxis_title="Win rate (%)",
        yaxis_range=[0, 100],
        xaxis_title="Week",
        height=420,
        margin=dict(l=10, r=10, t=20, b=10),
    )
    st.plotly_chart(fig, width="stretch")

    # --- Weekly table ------------------------------------------------------
    disp = weekly.copy()
    disp["win_rate"] = disp["win_rate"].map(lambda v: fmt_pct(v, decimals=1))
    st.dataframe(disp, width="stretch", hide_index=True)
