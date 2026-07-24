"""Call log view — detailed per-channel table of every call and its outcome."""
from __future__ import annotations

import streamlit as st

from db import get_connection
from ui.formatting import fmt_pct, fmt_price


_STATUS_BADGE = {
    "win": "🟢 WIN",
    "loss": "🔴 LOSS",
    "pending": "⏳ PENDING",
}


def _channel_options():
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, title, username FROM channels ORDER BY title"
    ).fetchall()
    opts = []
    for r in rows:
        lbl = r["title"] or r["username"] or f"Channel {r['id']}"
        if r["username"]:
            lbl = f"{lbl} (@{r['username']})"
        opts.append((lbl, r["id"]))
    return opts


def render() -> None:
    st.header("📋 Call Log")

    opts = _channel_options()
    if not opts:
        st.info("No channels yet. Run a backfill from the sidebar first.")
        return

    label, channel_id = st.selectbox(
        "Select a channel", opts, format_func=lambda x: x[0], key="calllog_pick"
    )

    # Filters
    fc1, fc2 = st.columns(2)
    with fc1:
        status_filter = st.multiselect(
            "Filter by status",
            options=list(_STATUS_BADGE.keys()),
            default=[],
            format_func=lambda s: _STATUS_BADGE.get(s, s),
        )
    with fc2:
        wins_only = st.checkbox("Wins only", value=False)

    conn = get_connection()
    query = """
        SELECT call_timestamp, token_symbol, token_name, token_address,
               entry_price_usd, peak_price_usd, peak_timestamp,
               peak_profit_pct, is_win, status, week_index
        FROM calls
        WHERE channel_id = ?
          AND status != 'unpriceable_loss'
    """
    params: list = [channel_id]
    if status_filter:
        placeholders = ",".join("?" * len(status_filter))
        query += f" AND status IN ({placeholders})"
        params.extend(status_filter)
    if wins_only:
        query += " AND is_win = 1"
    query += " ORDER BY call_timestamp DESC"

    rows = conn.execute(query, params).fetchall()
    if not rows:
        st.info("No calls match the current filters.")
        return

    import pandas as pd

    df = pd.DataFrame(rows)
    df["token"] = df["token_symbol"].fillna(df["token_name"]).fillna(df["token_address"])
    df["result"] = df["status"].map(lambda s: _STATUS_BADGE.get(s, s))
    df["profit"] = df["peak_profit_pct"].map(
        lambda v: fmt_pct(v, sign=True, decimals=0)
    )
    df["entry_usd"] = df["entry_price_usd"].map(lambda v: fmt_price(v))
    df["peak_usd"] = df["peak_price_usd"].map(lambda v: fmt_price(v))

    st.dataframe(
        df[
            [
                "call_timestamp",
                "token",
                "week_index",
                "result",
                "entry_usd",
                "peak_usd",
                "profit",
            ]
        ].rename(
            columns={
                "call_timestamp": "Date/time (UTC)",
                "token": "Token",
                "week_index": "Week",
                "result": "Result",
                "entry_usd": "Entry price",
                "peak_usd": "Peak price",
                "profit": "Peak profit",
            }
        ),
        width="stretch",
        hide_index=True,
    )

    csv = df.to_csv(index=False).encode()
    st.download_button(
        "⬇️ Download call log (CSV)",
        csv,
        file_name=f"call_log_channel_{channel_id}.csv",
        mime="text/csv",
    )
