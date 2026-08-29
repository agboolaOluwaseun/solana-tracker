"""Call Log view — a global chronological log of every call across all channels.

Shows when a call was made and what it was, not its win/loss outcome (that is
the leaderboard/deep-dive's job). One log for all channels.
"""
from __future__ import annotations

import streamlit as st

from db import get_connection


def _channel_options():
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, title, username FROM channels ORDER BY title"
    ).fetchall()
    opts = [(-1, "All channels")]
    for r in rows:
        lbl = r["title"] or r["username"] or f"Channel {r['id']}"
        if r["username"]:
            lbl = f"{lbl} (@{r['username']})"
        opts.append((r["id"], lbl))
    return opts


def render() -> None:
    st.header("📋 Call Log")
    st.caption(
        "Every call across all channels — what was called and when. "
        "For win/loss outcomes, see the Leaderboard or Channel Deep-Dive."
    )

    conn = get_connection()
    opts = _channel_options()
    filter_ch = st.selectbox(
        "Channel", opts, format_func=lambda x: x[1], index=0, key="calllog_pick"
    )
    channel_id = filter_ch[0]

    query = """
        SELECT cal.call_timestamp, ch.title AS channel_title,
               ch.username AS channel_username,
               cal.token_symbol, cal.token_name, cal.token_address, cal.week_index
        FROM calls cal
        LEFT JOIN channels ch ON ch.id = cal.channel_id
    """
    params: list = []
    if channel_id != -1:
        query += " WHERE cal.channel_id = ?"
        params.append(channel_id)
    query += " ORDER BY cal.call_timestamp DESC"

    rows = conn.execute(query, params).fetchall()
    if not rows:
        st.info("No calls logged yet. Run a backfill from the sidebar to populate the log.")
        return

    import pandas as pd

    df = pd.DataFrame(rows)
    df["token"] = df["token_symbol"].fillna(df["token_name"]).fillna(df["token_address"])
    df["channel"] = df["channel_title"].fillna(df["channel_username"]).fillna("?")

    st.dataframe(
        df[
            [
                "call_timestamp",
                "channel",
                "token",
                "token_address",
                "week_index",
            ]
        ].rename(
            columns={
                "call_timestamp": "Date/time (UTC)",
                "channel": "Channel",
                "token": "Token",
                "token_address": "Address",
                "week_index": "Week",
            }
        ),
        width="stretch",
        hide_index=True,
    )

    csv = df.to_csv(index=False).encode()
    st.download_button(
        "⬇️ Download call log (CSV)",
        csv,
        file_name="call_log.csv",
        mime="text/csv",
    )