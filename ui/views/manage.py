"""Manage view — channel table, re-price, delete, review, and manual override actions."""
from __future__ import annotations

import streamlit as st

from db import get_connection, transaction
from ui.formatting import fmt_pct, fmt_price


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


def _unresolved_calls(channel_id: int) -> list[dict]:
    """Return pending + unpriceable calls for a channel."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT id, token_symbol, token_address, call_timestamp, status, raw_text
        FROM calls
        WHERE channel_id = ? AND status IN ('pending', 'unpriceable_loss')
        ORDER BY call_timestamp ASC
        """,
        (channel_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def render() -> None:
    st.header("⚙️ Manage Channels")
    st.caption("Re-price interrupted calls, delete channel data, or remove channels entirely.")

    if "delete_message" in st.session_state:
        st.success(st.session_state["delete_message"])
        del st.session_state["delete_message"]

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
        losses_n = ch["losses"] or 0
        total_n = ch["total"] or 0

        with st.expander(f"🎛️ {name} — {total_n} calls ({pending_n} pending, {unpriceable_n} unpriceable)"):

            # --- Re-price section ---
            c1, c2 = st.columns(2)
            with c1:
                if pending_n > 0:
                    if st.button(f"🔄 Re-price {pending_n} pending", key=f"reprice_pending_{ch_id}", type="primary"):
                        _run_reprice(ch_id, ["pending"], name)
                else:
                    st.info("✅ No pending calls to re-price.")
            with c2:
                if unpriceable_n > 0:
                    if st.button(f"🔄 Re-price {unpriceable_n} unpriceable", key=f"reprice_unpriceable_{ch_id}"):
                        _run_reprice(ch_id, ["unpriceable_loss"], name)
                else:
                    st.info("✅ No unpriceable calls to re-price.")

            # --- Delete section ---
            st.markdown("---")
            st.markdown("**Delete Options:**")

            status_col1, status_col2, status_col3 = st.columns(3)
            with status_col1:
                if losses_n > 0:
                    if st.button(f"🗑️ Delete {losses_n} losses", key=f"del_losses_{ch_id}"):
                        _run_delete_by_status(ch_id, name, ["loss"])
                else:
                    st.caption("No losses")
            with status_col2:
                if pending_n > 0:
                    if st.button(f"🗑️ Delete {pending_n} pending", key=f"del_pending_{ch_id}"):
                        _run_delete_by_status(ch_id, name, ["pending"])
                else:
                    st.caption("No pending")
            with status_col3:
                if unpriceable_n > 0:
                    if st.button(f"🗑️ Delete {unpriceable_n} unpriceable", key=f"del_unpriceable_{ch_id}"):
                        _run_delete_by_status(ch_id, name, ["unpriceable_loss"])
                else:
                    st.caption("No unpriceable")

            st.markdown("---")
            confirm_key = f"confirm_delete_{ch_id}"
            confirm = st.checkbox(
                f"I understand this will permanently delete all data for **{name}**",
                key=confirm_key,
            )
            d1, d2 = st.columns(2)
            with d1:
                if st.button(f"🗑️ Delete all calls", key=f"del_calls_{ch_id}", disabled=(not confirm)):
                    _run_delete(ch_id, name, delete_channel=False)
            with d2:
                if st.button(f"🗑️ Delete channel entirely", key=f"del_chan_{ch_id}", disabled=(not confirm)):
                    _run_delete(ch_id, name, delete_channel=True)

            # --- Review unresolved calls ---
            unresolved = _unresolved_calls(ch_id)
            if unresolved:
                st.markdown("---")
                st.markdown(f"**🔍 Review {len(unresolved)} Unresolved Calls**")
                st.caption("Select calls to re-price with Birdeye or manually override their status.")

                for call in unresolved:
                    call_id = call["id"]
                    sym = call["token_symbol"] or call["token_address"][:8] + "…"
                    ts = call["call_timestamp"]
                    status = call["status"]
                    badge = "⏳ PENDING" if status == "pending" else "⚫ UNPRICEABLE"
                    addr = call["token_address"]

                    # Show the raw message for context
                    raw = (call.get("raw_text") or "")[:120]
                    raw_display = raw + ("…" if len(raw) >= 120 else "")

                    # Checkbox for Birdeye selection
                    selected = st.checkbox(
                        f"{badge} **{sym}** — {ts} — `{addr[:8]}…{addr[-4:]}`",
                        key=f"select_{call_id}",
                        help=raw_display,
                    )

                    # Show the raw message in a small caption
                    if raw_display:
                        st.caption(f"> {raw_display}")

                    # Manual override buttons
                    oc1, oc2, oc3 = st.columns(3)
                    with oc1:
                        if st.button("✅ Mark WIN", key=f"override_win_{call_id}"):
                            _run_override(call_id, "win")
                    with oc2:
                        if st.button("❌ Mark LOSS", key=f"override_loss_{call_id}"):
                            _run_override(call_id, "loss")
                    with oc3:
                        if st.button("🗑️ Delete", key=f"override_del_{call_id}"):
                            _run_delete_single(call_id, sym)

                # --- Birdeye re-price selected ---
                selected_ids = [
                    call["id"]
                    for call in unresolved
                    if st.session_state.get(f"select_{call['id']}", False)
                ]
                if selected_ids:
                    st.markdown("---")
                    st.info(f"🐦 {len(selected_ids)} call(s) selected for Birdeye re-pricing")
                    if st.button(
                        f"🐦 Re-price {len(selected_ids)} selected with Birdeye",
                        key=f"birdeye_{ch_id}",
                        type="primary",
                    ):
                        _run_birdeye_reprice(ch_id, selected_ids, name)
                else:
                    st.caption("Select calls above ☝️ to re-price with Birdeye")


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
            st.session_state["delete_message"] = (
                f"✅ Deleted **{name}**: {result['calls_deleted']} calls, "
                f"{result['runs_deleted']} runs, channel removed."
            )
        else:
            st.session_state["delete_message"] = (
                f"✅ Deleted {result['calls_deleted']} calls and "
                f"{result['runs_deleted']} runs for **{name}**. Channel retained."
            )
        st.session_state[f"confirm_delete_{ch_id}"] = False
        st.rerun()
    except Exception as e:
        st.error(f"Delete failed: {e}")


def _run_delete_by_status(ch_id: int, name: str, statuses: list[str]) -> None:
    """Execute status-specific deletion and show result."""
    from pipeline import delete_calls_by_status

    try:
        result = delete_calls_by_status(ch_id, statuses)
        status_label = ", ".join(s.replace("_loss", "") for s in statuses)
        st.session_state["delete_message"] = (
            f"✅ Deleted {result['calls_deleted']} {status_label} call(s) for **{name}**."
        )
        st.rerun()
    except Exception as e:
        st.error(f"Delete failed: {e}")


def _run_delete_single(call_id: int, sym: str) -> None:
    """Delete a single call by ID."""
    conn = get_connection()
    with transaction() as c:
        c.execute("DELETE FROM calls WHERE id = ?", (call_id,))
    st.session_state["delete_message"] = f"✅ Deleted call **{sym}** (id={call_id})"
    st.rerun()


def _run_override(call_id: int, new_status: str) -> None:
    """Manually override a call's status."""
    from pipeline import manual_override_call

    try:
        result = manual_override_call(call_id, new_status)
        sym = result["token_address"][:8] + "…"
        old = result["old_status"]
        st.session_state["delete_message"] = (
            f"✅ Override: {sym} ({result['call_id']}) {old} → **{new_status}**"
        )
        st.rerun()
    except Exception as e:
        st.error(f"Override failed: {e}")


def _run_birdeye_reprice(ch_id: int, call_ids: list[int], name: str) -> None:
    """Re-price selected calls using Birdeye."""
    from pipeline import reprice_with_birdeye, Progress

    status = st.status(f"🐦 Birdeye re-pricing {len(call_ids)} calls for {name}…", expanded=True)
    try:
        def cb(p: Progress):
            if p.stage == "price":
                status.update(label=(
                    f"Birdeye: {p.scanned}/{p.total_calls} — "
                    f"{p.priced} priced, {p.unpriceable} still unpriceable…"
                ))
            elif p.stage == "done":
                status.update(label=p.message, state="complete")

        reprice_with_birdeye(ch_id, call_ids=call_ids, progress_cb=cb)
    except Exception as e:
        status.update(label=f"❌ {e}", state="error")
    finally:
        status.update(expanded=False)
    st.rerun()
