"""Manage view — channel table with re-price and delete actions."""
from __future__ import annotations

import streamlit as st

from db import get_connection
from ui.formatting import fmt_pct


def _channel_summary() -> list[dict]:
    """Return per-channel summary: call counts by status + win rate."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT c.id, c.title, c.username, c.window_start, c.window_end,
               COUNT(cal.id) AS total,
               SUM(CASE WHEN cal.status = 'win' THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN cal.status = 'loss' THEN 1 ELSE 0 END) AS losses,
               SUM(CASE WHEN cal.status = 'pending' THEN 1 ELSE 0 END) AS pending,
               SUM(CASE WHEN cal.status = 'unpriceable_loss' THEN 1 ELSE 0 END) AS unpriceable,
               AVG(CASE WHEN cal.status IN ('win','loss') THEN cal.peak_profit_pct END) AS avg_peak
        FROM channels c
        LEFT JOIN calls cal ON cal.channel_id = c.id
        GROUP BY c.id
        ORDER BY c.title
        """
    ).fetchall()
    return [dict(r) for r in rows]


def render() -> None:
    st.header("⚙️ Manage Channels")
    st.caption("Re-price interrupted calls, delete channel data, or remove channels entirely.")

    channels = _channel_summary()
    if not channels:
        st.info("No channels in the database. Run a backfill first.")
        return

    # --- Summary table ---
    import pandas as pd

    display = []
    for ch in channels:
        decided = (ch["wins"] or 0) + (ch["losses"] or 0)
        wr = (ch["wins"] or 0) / decided * 100 if decided else None
        display.append({
            "ID": ch["id"],
            "Channel": ch["title"] or ch["username"] or f"Channel {ch['id']}",
            "Username": f"@{ch['username']}" if ch["username"] else "—",
            "Window": f"{(ch['window_start'] or '?')[:10]} → {(ch['window_end'] or '?')[:10]}",
            "Total": ch["total"] or 0,
            "Wins": ch["wins"] or 0,
            "Losses": ch["losses"] or 0,
            "Pending": ch["pending"] or 0,
            "Unpriceable": ch["unpriceable"] or 0,
            "Win Rate": fmt_pct(wr, decimals=1),
        })

    df = pd.DataFrame(display)
    st.dataframe(df, width="stretch", hide_index=True)

    st.divider()

    # --- Per-channel actions ---
    st.subheader("Actions")

    for ch in channels:
        ch_id = ch["id"]
        name = ch["title"] or ch["username"] or f"Channel {ch_id}"
        pending_n = ch["pending"] or 0
        unpriceable_n = ch["unpriceable"] or 0
        total_n = ch["total"] or 0

        with st.expander(f"🎛️ {name} — {total_n} calls ({pending_n} pending, {unpriceable_n} unpriceable)"):

            # --- Re-price section ---
            c1, c2 = st.columns(2)
            with c1:
                if pending_n > 0:
                    if st.button(
                        f"🔄 Re-price {pending_n} pending",
                        key=f"reprice_pending_{ch_id}",
                        type="primary",
                    ):
                        _run_reprice(ch_id, ["pending"], name)
                else:
                    st.info("✅ No pending calls to re-price.")
            with c2:
                if unpriceable_n > 0:
                    if st.button(
                        f"🔄 Re-price {unpriceable_n} unpriceable",
                        key=f"reprice_unpriceable_{ch_id}",
                    ):
                        _run_reprice(ch_id, ["unpriceable_loss"], name)
                else:
                    st.info("✅ No unpriceable calls to re-price.")

            # --- Delete section ---
            st.markdown("---")
            confirm_key = f"confirm_delete_{ch_id}"
            confirm = st.checkbox(
                f"I understand this will permanently delete all data for **{name}**",
                key=confirm_key,
            )
            d1, d2 = st.columns(2)
            with d1:
                if st.button(
                    f"🗑️ Delete calls only",
                    key=f"del_calls_{ch_id}",
                    disabled=(not confirm),
                ):
                    _run_delete(ch_id, name, delete_channel=False)
            with d2:
                if st.button(
                    f"🗑️ Delete channel entirely",
                    key=f"del_chan_{ch_id}",
                    disabled=(not confirm),
                ):
                    _run_delete(ch_id, name, delete_channel=True)


def _run_reprice(ch_id: int, statuses: list[str], name: str) -> None:
    """Execute re-pricing with live progress."""
    from pipeline import reprice_calls, Progress

    status = st.status(f"Re-pricing {name}…", expanded=True)
    try:
        def cb(p: Progress):
            if p.stage == "price":
                status.update(label=(
                    f"Re-pricing {name}: {p.scanned}/{p.total_calls} — "
                    f"{p.priced} priced, {p.unpriceable} still unpriceable…"
                ))
            elif p.stage == "done":
                status.update(label=p.message, state="complete")

        reprice_calls(ch_id, statuses=statuses, progress_cb=cb)
    except Exception as e:
        status.update(label=f"Error: {e}", state="error")
    finally:
        status.update(expanded=False)
    st.rerun()


def _run_delete(ch_id: int, name: str, delete_channel: bool) -> None:
    """Execute deletion and show result."""
    from pipeline import delete_channel_data

    try:
        result = delete_channel_data(ch_id, delete_channel=delete_channel)
        if delete_channel:
            st.success(
                f"✅ Deleted **{name}**: {result['calls_deleted']} calls, "
                f"{result['runs_deleted']} runs, channel removed."
            )
        else:
            st.success(
                f"✅ Deleted {result['calls_deleted']} calls and "
                f"{result['runs_deleted']} runs for **{name}**. Channel retained."
            )
        st.rerun()
    except Exception as e:
        st.error(f"Delete failed: {e}")
