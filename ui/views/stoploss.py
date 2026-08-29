"""50% Stop-Loss Strategy view — compare the normal strategy vs stop-loss strategy."""
from __future__ import annotations

import streamlit as st
import pandas as pd

from db import get_connection, transaction
from ui.formatting import fmt_price, fmt_pct, fmt_ts


def _get_stoploss_results() -> list[dict]:
    """Return stoploss results joined with call/channel data."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT
            sr.id AS sr_id,
            sr.call_id,
            sr.entry_price_usd AS sl_entry,
            sr.peak_price_usd AS sl_peak,
            sr.peak_profit_pct AS sl_peak_pct,
            sr.hit_stoploss,
            sr.stoploss_timestamp,
            sr.is_win AS sl_win,
            sr.status AS sl_status,
            sr.error AS sl_error,
            c.token_symbol,
            c.token_address,
            c.call_timestamp,
            c.entry_price_usd AS call_entry,
            c.peak_price_usd AS call_peak,
            c.peak_profit_pct AS call_peak_pct,
            c.is_win AS call_win,
            c.status AS call_status,
            ch.title AS channel_title,
            ch.username AS channel_username
        FROM stoploss_results sr
        JOIN calls c ON c.id = sr.call_id
        LEFT JOIN channels ch ON ch.id = c.channel_id
        ORDER BY c.call_timestamp DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _get_stoploss_summary() -> dict:
    """Return aggregate metrics for the stop-loss strategy."""
    conn = get_connection()
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN sr.status = 'win' THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN sr.status = 'loss' THEN 1 ELSE 0 END) AS losses,
            SUM(CASE WHEN sr.status = 'unpriceable_loss' THEN 1 ELSE 0 END) AS unpriceable,
            AVG(CASE WHEN sr.status IN ('win','loss') THEN sr.peak_profit_pct END) AS avg_peak
        FROM stoploss_results sr
        """
    ).fetchone()
    return dict(row) if row else {}


def render() -> None:
    st.header("📉 50% Stop-Loss Strategy")
    st.caption(
        "Compare the **normal strategy** (win = peak ≥ 2x within 12h) vs the "
        "**-50% stop-loss strategy** (win = reached 2x without first dropping 50%). "
        "Both are computed from the same 12h × 1-minute candles during pricing — "
        "no extra API calls."
    )

    # --- Summary ---
    summary = _get_stoploss_summary()
    if summary.get("total", 0) > 0:
        total = summary["total"]
        wins = summary["wins"] or 0
        losses = summary["losses"] or 0
        unpriceable = summary["unpriceable"] or 0
        decided = wins + losses
        wr = (wins / decided * 100) if decided > 0 else None

        st.subheader("Summary")
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Total Calls", total)
        m2.metric("Wins", wins)
        m3.metric("Losses", losses)
        m4.metric("Win Rate", fmt_pct(wr, decimals=1))
        m5.metric("Avg Peak", fmt_pct(summary.get("avg_peak"), decimals=1))
    else:
        st.info("No stop-loss results yet. Run a backfill to populate both strategies.")
        return

    # --- Results table ---
    results = _get_stoploss_results()
    if results:
        st.subheader("Results")

        # Channel filter
        all_channels = sorted(set(r["channel_title"] or r["channel_username"] or "?" for r in results))
        filter_ch = st.selectbox("Filter by channel", ["All"] + all_channels, key="sl_filter_ch")

        # Status filter
        filter_status = st.selectbox(
            "Filter by status",
            ["All", "Win", "Loss", "Stop-loss Hit", "Stop-loss Avoided", "Error"],
            key="sl_filter_status",
        )

        rows = []
        for r in results:
            ch_name = r["channel_title"] or r["channel_username"] or "?"
            if filter_ch != "All" and ch_name != filter_ch:
                continue

            sl_status = r["sl_status"]
            sl_win = r["sl_win"]
            call_win = r["call_win"]
            hit_sl = r["hit_stoploss"]

            if filter_status == "Win" and sl_status != "win":
                continue
            if filter_status == "Loss" and sl_status != "loss":
                continue
            if filter_status == "Stop-loss Hit" and not hit_sl:
                continue
            if filter_status == "Stop-loss Avoided" and hit_sl:
                continue
            if filter_status == "Error" and not r["sl_error"]:
                continue

            sym = r["token_symbol"] or r["token_address"][:8] + "…"

            if sl_status == "win":
                sl_badge = "🟢 WIN"
            elif sl_status == "loss":
                if hit_sl:
                    sl_badge = "🔴 STOP-LOSS"
                else:
                    sl_badge = "🔴 LOSS (no stop)"
            else:
                sl_badge = "⚫ ERROR"

            if call_win:
                call_badge = "🟢 WIN"
            else:
                call_badge = "🔴 LOSS"

            rows.append({
                "Channel": ch_name,
                "Token": sym,
                "Call Date": fmt_ts(r["call_timestamp"]),
                "Normal": call_badge,
                "Normal Peak": fmt_pct(r["call_peak_pct"], decimals=1),
                "Stop-Loss": sl_badge,
                "SL Peak": fmt_pct(r["sl_peak_pct"], decimals=1),
                "Hit SL": "✅" if hit_sl else "❌",
                "SL Time": fmt_ts(r["stoploss_timestamp"]) if r["stoploss_timestamp"] else "—",
            })

        if rows:
            df = pd.DataFrame(rows)
            st.dataframe(df, width="stretch", hide_index=True, use_container_width=True)
        else:
            st.info("No results match the current filters.")

        # --- Comparison chart ---
        st.subheader("Strategy Comparison")
        label_a, label_b, label_c = st.columns(3)
        with label_a:
            normal_wins = sum(1 for r in results if r["call_win"])
            normal_losses = sum(1 for r in results if r["call_status"] == "loss" and not r["call_win"])
            normal_total = normal_wins + normal_losses
            normal_wr = (normal_wins / normal_total * 100) if normal_total > 0 else 0
        with label_b:
            sl_wins = sum(1 for r in results if r["sl_status"] == "win")
            sl_losses = sum(1 for r in results if r["sl_status"] == "loss")
            sl_total = sl_wins + sl_losses
            sl_wr = (sl_wins / sl_total * 100) if sl_total > 0 else 0
        with label_c:
            saved = sum(1 for r in results if r["call_win"] == 0 and r["sl_status"] == "win")
            missed = sum(1 for r in results if r["call_win"] == 1 and r["sl_status"] == "loss")

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Normal Win Rate", f"{normal_wr:.1f}%", delta=f"{normal_wins}/{normal_total}")
        m2.metric("Stop-Loss Win Rate", f"{sl_wr:.1f}%", delta=f"{sl_wins}/{sl_total}")
        delta = round(sl_wr - normal_wr, 1)
        m3.metric("Difference", f"{delta:+.1f}%", delta_color="inverse" if delta < 0 else "normal")
        m4.metric("Losses Saved → Wins", saved, delta=f"{missed} wins lost")


def _get_channels() -> list[dict]:
    """Return list of {id, label} for channels in the DB."""
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT id, title, username FROM channels ORDER BY title"
        ).fetchall()
        return [
            {
                "id": r["id"],
                "label": r["title"] or r["username"] or f"Channel {r['id']}",
            }
            for r in rows
        ]
    except Exception:
        return []


def _run_backtest(channel_id: int | None) -> None:
    """Run the stop-loss backtest with progress."""
    from pipeline import run_stoploss_backtest, Progress

    status = st.status("Running stop-loss backtest…", expanded=True)
    try:
        def cb(p: Progress):
            if p.stage == "price":
                status.update(label=(
                    f"💲 {p.scanned}/{p.total_calls} — "
                    f"✅ {p.priced} done, ⚫ {p.unpriceable} errors"
                ))
            elif p.stage == "done":
                status.update(label=p.message, state="complete")

        run_stoploss_backtest(channel_id=channel_id, progress_cb=cb)
    except Exception as e:
        status.update(label=f"❌ {e}", state="error")
    finally:
        status.update(expanded=False)
    st.rerun()