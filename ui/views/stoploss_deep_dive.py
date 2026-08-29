"""Strategy 2 (-50%) Channel Deep-Dive view — weekly stop-loss success rates."""
from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from analysis.stoploss import (
    stoploss_channel_detail,
    stoploss_overall,
    stoploss_weekly,
)
from db import get_connection
from ui.formatting import fmt_pct


def _channel_options():
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
    st.header("🔎 Channel Deep-Dive (-50%)")
    st.caption(
        "Strategy-2 outcomes: win = reached 2x without first dropping 50% (within 12h)."
    )

    opts = _channel_options()
    if not opts:
        st.info("No channels yet. Run a backfill from the sidebar first.")
        return

    label, channel_id = st.selectbox(
        "Select a channel", opts, format_func=lambda x: x[0], key="sldd_pick"
    )

    detail = stoploss_channel_detail(channel_id)
    if not detail or detail["total_calls"] == 0:
        st.warning("No strategy-2 data for this channel yet.")
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total calls", detail["total_calls"])
    c2.metric("Wins", detail["wins"])
    wr = detail["win_rate"]
    c3.metric("Overall win rate (-50%)", fmt_pct(wr, decimals=1))
    avg = detail["avg_peak_profit_pct"]
    c4.metric("Avg peak profit", fmt_pct(avg, sign=True, decimals=0))

    st.caption(
        f"Window: {detail.get('window_start', '?')} → {detail.get('window_end', '?')}"
    )

    weekly = stoploss_weekly(channel_id)
    st.subheader("Weekly success rate (-50%)")

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=[f"W{int(w)}" for w in weekly["week_index"]],
            y=weekly["win_rate"],
            text=weekly["total_calls"].map(lambda n: f"{n} calls" if n else "—"),
            textposition="outside",
            marker_color="#FFA500",
            name="Win rate (-50%)",
        )
    )
    fig.add_hline(
        y=wr if wr is not None else 0,
        line_dash="dash",
        line_color="#00C19F",
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

    disp = weekly.copy()
    disp["win_rate"] = disp["win_rate"].map(lambda v: fmt_pct(v, decimals=1))
    st.dataframe(disp, width="stretch", hide_index=True)