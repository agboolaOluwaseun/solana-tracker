"""
Investigation view — review and resolve pending/unpriceable calls.

Shows a filterable table of calls that haven't been successfully priced,
with per-row actions:
  - 🔍 Re-price with Birdeye (for unpriceable calls)
  - ✅ Mark as Win (manual override)
  - ❌ Mark as Loss (manual override)
  - ⏸ Reset to Pending
  - 🗑️ Delete this call
"""
from __future__ import annotations

import streamlit as st
import pandas as pd

from db import get_connection
from ui.formatting import fmt_price, fmt_pct


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


_STATUS_ICONS = {
    "pending": "⏳",
    "unpriceable_loss": "⚫",
    "win": "🟢",
    "loss": "🔴",
}


def render() -> None:
    st.header("🔬 Investigation — Pending & Unpriceable Calls")
    st.caption(
        "Review calls that couldn't be automatically priced. "
        "Re-price with Birdeye, manually override status, or delete bad entries."
    )

    # Show any pending message from a previous action
    if "investigation_message" in st.session_state:
        msg = st.session_state.pop("investigation_message")
        if msg.startswith("❌"):
            st.error(msg)
        else:
            st.success(msg)

    opts = _channel_options()
    if not opts:
        st.info("No channels in the database yet.")
        return

    # Channel selector
    label_index = st.selectbox(
        "Select a channel",
        range(len(opts)),
        format_func=lambda i: opts[i][0],
        key="investigation_pick",
    )
    label, channel_id = opts[label_index]

    # Status filter
    status_filter = st.multiselect(
        "Filter by status",
        options=["pending", "unpriceable_loss"],
        default=["pending", "unpriceable_loss"],
        format_func=lambda s: f"{_STATUS_ICONS.get(s, '')} {s.replace('_', ' ').title()}",
    )

    if not status_filter:
        st.warning("Select at least one status to investigate.")
        return

    # Fetch calls
    conn = get_connection()
    placeholders = ",".join("?" * len(status_filter))
    rows = conn.execute(
        f"""SELECT id, call_timestamp, token_symbol, token_name, token_address,
                   entry_price_usd, peak_price_usd, peak_profit_pct, status,
                   week_index, raw_text, priced_at
            FROM calls
            WHERE channel_id = ? AND status IN ({placeholders})
            ORDER BY call_timestamp DESC""",
        [channel_id, *status_filter],
    ).fetchall()

    if not rows:
        st.success("No calls match the current filters — all clear! 🎉")
        return

    # Build DataFrame
    df = pd.DataFrame(rows)
    df["token"] = df["token_symbol"].fillna(df["token_name"]).fillna(df["token_address"].str[:12] + "…")
    df["status_icon"] = df["status"].map(lambda s: _STATUS_ICONS.get(s, "?"))
    df["entry"] = df["entry_price_usd"].map(fmt_price)
    df["peak"] = df["peak_price_usd"].map(fmt_price)
    df["profit"] = df["peak_profit_pct"].map(lambda v: fmt_pct(v, sign=True, decimals=1) if pd.notna(v) else "—")

    # Display table
    st.subheader(f"{len(df)} calls to review")

    display_cols = [
        "status_icon", "call_timestamp", "token", "token_address",
        "week_index", "entry", "peak", "profit", "raw_text"
    ]
    display_df = df[display_cols].copy()
    display_df.columns = [
        "Status", "Called (UTC)", "Token", "Address",
        "Week", "Entry", "Peak", "Profit", "Message"
    ]
    st.dataframe(
        display_df,
        width="stretch",
        hide_index=True,
        column_config={
            "Message": st.column_config.TextColumn(width="large"),
            "Address": st.column_config.TextColumn(width="medium"),
        },
    )

    st.divider()

    # ── Per-row actions ──────────────────────────────────────────────────
    st.subheader("Actions")

    # Bulk Birdeye re-price
    unpriceable_ids = df[df["status"] == "unpriceable_loss"]["id"].tolist()
    if unpriceable_ids:
        c1, c2 = st.columns([1, 4])
        with c1:
            if st.button("🔍 Birdeye re-price all", type="primary", key="invest_birdeye_all"):
                _run_birdeye_reprice(channel_id, name=label, call_ids=None)
        with c2:
            st.caption(f"Will re-price {len(unpriceable_ids)} unpriceable calls via Birdeye API")

    st.divider()

    # Individual row actions
    st.markdown("**Individual call actions:**")

    # Show only first 20 rows with actions to avoid rendering too many buttons
    action_df = df.head(20)
    if len(df) > 20:
        st.caption(f"Showing actions for first 20 of {len(df)} calls. Use filters to narrow down.")

    for idx, action_row in action_df.iterrows():
        # Extract scalars — iterrows returns scalar values at runtime
        status_val = action_row["status"]  # type: ignore
        token_val = action_row["token"]  # type: ignore
        ts_raw = action_row["call_timestamp"]  # type: ignore
        addr_val = action_row["token_address"]  # type: ignore
        call_id_val = int(action_row["id"])  # type: ignore
        raw_val = action_row["raw_text"]  # type: ignore
        if pd.isna(raw_val):
            raw_val = ""
        ts_val = str(ts_raw)[:16]

        with st.expander(
            f"{_STATUS_ICONS.get(status_val, '?')} {token_val or 'Unknown'} — "
            f"{ts_val} UTC — {addr_val[:12]}…"
        ):
            # Show message context
            st.markdown(f"**Message:** {raw_val[:500] if raw_val else '—'}")

            col1, col2, col3, col4, col5 = st.columns(5)

            # Birdeye re-price (only for unpriceable)
            with col1:
                if status_val == "unpriceable_loss":
                    if st.button(
                        "🔍 Birdeye",
                        key=f"inv_birdeye_{call_id_val}",
                        help="Re-price this call using Birdeye API",
                    ):
                        _run_birdeye_reprice_single(call_id_val, addr_val, token_val or "token")

            # Manual overrides
            with col2:
                if st.button(
                    "✅ Win",
                    key=f"inv_win_{call_id_val}",
                    help="Manually mark as win",
                    type="primary" if status_val == "win" else "secondary",
                ):
                    _run_manual_override(call_id_val, addr_val, "win")
            with col3:
                if st.button(
                    "❌ Loss",
                    key=f"inv_loss_{call_id_val}",
                    help="Manually mark as loss",
                    type="secondary",
                ):
                    _run_manual_override(call_id_val, addr_val, "loss")
            with col4:
                if st.button(
                    "⏸ Pending",
                    key=f"inv_pending_{call_id_val}",
                    help="Reset to pending",
                ):
                    _run_manual_override(call_id_val, addr_val, "pending")
            with col5:
                if st.button(
                    "🗑️",
                    key=f"inv_del_{call_id_val}",
                    help="Delete this call",
                ):
                    _run_delete_call(call_id_val)


def _run_birdeye_reprice(channel_id: int, name: str, call_ids: list[int] | None) -> None:
    """Bulk Birdeye re-price with progress."""
    from pipeline import reprice_with_birdeye, Progress

    status = st.status(f"🔍 Re-pricing with Birdeye for {name}…", expanded=True)

    def cb(p: Progress):
        if p.stage == "price":
            status.update(label=(
                f"🔍 Birdeye: {p.scanned}/{p.total_calls} — "
                f"✅ {p.priced} priced, ⚫ {p.unpriceable} still unpriceable"
            ))
        elif p.stage == "done":
            status.update(label=p.message, state="complete")

    try:
        reprice_with_birdeye(channel_id, call_ids=call_ids, progress_cb=cb)
    except Exception as e:
        status.update(label=f"❌ {e}", state="error")
    finally:
        status.update(expanded=False)

    st.session_state["investigation_message"] = f"✅ Birdeye re-price complete for {name}."
    st.rerun()


def _run_birdeye_reprice_single(call_id: int, token_addr: str, token_name: str) -> None:
    """Single call Birdeye re-price."""
    from pricing.birdeye import BirdeyeClient, BirdeyeError
    from pricing.backtest import score_candles
    from datetime import datetime, timedelta
    from config import settings

    status = st.status(f"🔍 Birdeye re-price for {token_name}…", expanded=True)

    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT channel_id, call_timestamp FROM calls WHERE id = ?", (call_id,)
        ).fetchone()

        call_ts = datetime.fromisoformat(row["call_timestamp"].replace("Z", ""))
        end_ts = call_ts + timedelta(hours=settings.peak_window_hours)

        client = BirdeyeClient()

        # Fetch candles
        minute_candles = client.fetch_ohlcv(
            token_addr,
            call_ts - timedelta(minutes=5),
            call_ts + timedelta(minutes=10),
            interval="1m",
        )
        hour_candles = client.fetch_ohlcv(
            token_addr,
            call_ts,
            end_ts,
            interval="1H",
        )

        all_candles = list(minute_candles) + list(hour_candles)
        result = score_candles(all_candles, call_ts, token_addr)

        # Update DB
        def iso(dt):
            return dt.replace(microsecond=0).isoformat() + "Z"

        from db import transaction
        with transaction() as conn:
            conn.execute(
                """UPDATE calls SET
                    pool_address = ?, entry_price_usd = ?, peak_price_usd = ?,
                    peak_timestamp = ?, peak_profit_pct = ?, is_win = ?,
                    status = ?, priced_at = datetime('now')
                WHERE id = ?""",
                (
                    result.pool_address or "birdeye",
                    result.entry_price_usd,
                    result.peak_price_usd,
                    iso(result.peak_timestamp) if result.peak_timestamp else None,
                    result.peak_profit_pct,
                    1 if result.is_win else 0,
                    result.status,
                    call_id,
                ),
            )

        if result.status == "unpriceable_loss":
            status.update(
                label=f"⚫ Still unpriceable: {result.error or 'no data'}",
                state="error",
            )
        else:
            profit_str = f"+{result.peak_profit_pct:.1f}%" if result.peak_profit_pct else "0%"
            badge = "✅ WIN" if result.is_win else "❌ LOSS"
            status.update(
                label=f"{badge} — Entry: ${result.entry_price_usd:.8f}, "
                      f"Peak: ${result.peak_price_usd:.8f} ({profit_str})",
                state="complete",
            )

        st.session_state["investigation_message"] = (
            f"✅ {token_name}: {result.status.replace('_', ' ').title()}"
            f"{' (WIN)' if result.is_win else ''}"
        )
    except BirdeyeError as e:
        status.update(label=f"❌ Birdeye error: {e}", state="error")
    except Exception as e:
        status.update(label=f"❌ {e}", state="error")
    finally:
        status.update(expanded=False)

    st.rerun()


def _run_manual_override(call_id: int, token_addr: str, new_status: str) -> None:
    """Manual status override with optional price input.

    Uses session_state to persist the override form across reruns so the
    number inputs don't disappear when the user interacts with them.
    """
    from pipeline import manual_override_call

    form_key = f"override_form_{call_id}_{new_status}"

    # Initialize form visibility in session state
    if form_key not in st.session_state:
        st.session_state[form_key] = False

    # Show price input for win/loss
    if new_status in ("win", "loss"):
        # If this is the first click, activate the form
        st.session_state[form_key] = True
        st.session_state[f"override_status_{call_id}"] = new_status

        with st.expander(f"Enter pricing details for {new_status.upper()}", expanded=True):
            c1, c2 = st.columns(2)
            with c1:
                entry_price = st.number_input(
                    "Entry price (USD)",
                    value=0.0,
                    format="%.10f",
                    key=f"override_entry_{call_id}",
                )
            with c2:
                peak_price = st.number_input(
                    "Peak price (USD)",
                    value=0.0,
                    format="%.10f",
                    key=f"override_peak_{call_id}",
                )

            # Auto-calculate profit if both prices set
            if entry_price > 0 and peak_price > 0:
                profit = (peak_price / entry_price - 1) * 100
                st.caption(f"Peak profit: {profit:+.1f}%")

            # Confirm
            confirm_key = f"confirm_override_{call_id}_{new_status}"
            confirm = st.checkbox(
                f"Confirm: mark as {new_status}", key=confirm_key
            )

            c_apply, c_cancel = st.columns(2)
            with c_apply:
                if st.button(
                    f"Apply: {new_status}",
                    key=f"apply_{call_id}_{new_status}",
                    disabled=(not confirm),
                    type="primary",
                ):
                    ep = entry_price if entry_price > 0 else None
                    pp = peak_price if peak_price > 0 else None
                    profit_pct = (pp / ep - 1) * 100 if ep and pp and ep > 0 else None

                    try:
                        result = manual_override_call(
                            call_id, new_status,
                            entry_price=ep, peak_price=pp, peak_profit_pct=profit_pct,
                        )
                        st.session_state["investigation_message"] = (
                            f"✅ {token_addr[:10]}…: {result['old_status']} → {result['new_status']}"
                        )
                        # Clear form state
                        st.session_state[form_key] = False
                        st.session_state.pop(f"override_status_{call_id}", None)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Override failed: {e}")
            with c_cancel:
                if st.button("Cancel", key=f"cancel_{call_id}_{new_status}"):
                    st.session_state[form_key] = False
                    st.session_state.pop(f"override_status_{call_id}", None)
                    st.rerun()
    else:
        # For pending/unpriceable — no pricing needed, apply immediately
        try:
            result = manual_override_call(call_id, new_status)
            st.session_state["investigation_message"] = (
                f"✅ {token_addr[:10]}…: {result['old_status']} → {result['new_status']}"
            )
            st.rerun()
        except Exception as e:
            st.error(f"Override failed: {e}")


def _run_delete_call(call_id: int) -> None:
    """Delete a single call."""
    from db import transaction

    try:
        with transaction() as conn:
            row = conn.execute(
                "SELECT token_address, status FROM calls WHERE id = ?", (call_id,)
            ).fetchone()

            if row:
                conn.execute("DELETE FROM calls WHERE id = ?", (call_id,))
                st.session_state["investigation_message"] = (
                    f"🗑️ Deleted call {call_id} ({row['token_address'][:10]}…, was {row['status']})"
                )
    except Exception as e:
        st.session_state["investigation_message"] = f"❌ Delete failed: {e}"

    st.rerun()
