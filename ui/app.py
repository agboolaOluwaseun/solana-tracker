"""
Streamlit entry point for solana_tracker.

Run with:
    streamlit run ui/app.py

Provides:
  - Sidebar: window start date, channel selector, "Run backfill" + refresh.
  - Three read-only views: Leaderboard, Channel Deep-Dive, Call Log.

The UI is a thin read layer over the scored SQLite tables; the heavy lifting
(fetch/parse/price) happens in the pipeline.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make the package root importable when running `streamlit run ui/app.py`.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st  # noqa: E402

from config import settings  # noqa: E402
from db import init_db  # noqa: E402
from ui.views import leaderboard, channel_deep_dive, call_log  # noqa: E402

st.set_page_config(
    page_title="Solana Caller Tracker",
    page_icon="🪙",
    layout="wide",
)

# Dark theme via a minimal CSS tweak (Streamlit picks dark mode from config;
# this ensures the header styling is consistent).
st.markdown(
    """
    <style>
      .block-container {padding-top: 1.5rem;}
      .stMetric {background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.06);
                 border-radius: 8px; padding: 0.75rem;}
    </style>
    """,
    unsafe_allow_html=True,
)


def run_backfill_action(channel_ref: str, start_date, title: str = None) -> None:
    """Trigger a backfill inline, streaming progress to the UI."""
    from pipeline import Progress, run_backfill

    start_dt = datetime.strptime(str(start_date), "%Y-%m-%d")
    end_dt = start_dt + timedelta(weeks=settings.window_weeks)
    username = channel_ref.lstrip("@") if channel_ref.startswith("@") else None

    status = st.status(
        f"Backfilling {channel_ref} ({start_dt.date()} → {end_dt.date()})…",
        expanded=True,
    )

    def cb(p: Progress):
        if p.stage == "fetch":
            status.update(label=f"[fetch] scanned {p.scanned} messages…")
        elif p.stage == "parse":
            status.update(label=f"[parse] {p.found} calls found…")
        elif p.stage == "price":
            status.update(
                label=(
                    f"[price] {p.scanned}/{p.total_calls} — "
                    f"{p.priced} priced, {p.unpriceable} unpriceable…"
                )
            )
        elif p.stage == "done":
            status.update(
                label=(
                    f"Done: {p.found} calls found, {p.priced} priced, "
                    f"{p.unpriceable} unpriceable."
                ),
                state="complete",
            )
        elif p.stage == "error":
            # Detect auth-related failures and give actionable guidance.
            msg = p.message or ""
            auth_hints = ("phone number", "bot token", "auth", "UNAUTHORIZED", "2FA")
            if any(h.lower() in msg.lower() for h in auth_hints):
                status.update(
                    label=(
                        "⚠️ Telegram login required. Streamlit can't prompt for a "
                        "login code. In a separate terminal, run:\n\n"
                        "    .venv/bin/python -m scripts.login\n\n"
                        "Then retry the backfill here."
                    ),
                    state="error",
                    expanded=True,
                )
            else:
                status.update(label=f"Error: {msg}", state="error", expanded=True)

    try:
        run_backfill(
            channel_ref=channel_ref,
            window_start=start_dt,
            window_end=end_dt,
            username=username,
            title=title,
            progress_cb=cb,
        )
    except Exception as e:
        status.update(label=f"Error: {e}", state="error")
    finally:
        status.update(expanded=False)


def main() -> None:
    init_db()

    st.title("🪙 Solana Caller Tracker")
    st.caption(
        "Historical Telegram → Solana call backtesting. "
        f"Win = peak ≥ {settings.win_multiplier}x within {settings.peak_window_hours}h of the call. "
        f"Rate limit: {settings.requests_per_minute} req/min (GeckoTerminal)."
    )

    # ---- Sidebar ----------------------------------------------------------
    with st.sidebar:
        st.header("⚙️ Configuration")
        st.write(f"**Window:** {settings.window_weeks} weeks")
        st.write(f"**Win threshold:** {settings.win_multiplier}x")
        st.write(f"**Peak window:** {settings.peak_window_hours}h")
        st.write(f"**Pricing:** GeckoTerminal ({settings.requests_per_minute} RPM)")

        st.divider()
        st.header("🚀 Run backfill")

        config_ok = settings.telegram_configured
        if not config_ok:
            st.warning(
                "Telegram not configured. Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env"
            )

        # ---- Channel picker: dropdown of channels you've joined ----------
        # Cache the dialog list in session_state so the dropdown is instant on
        # every rerun; the "Refresh channel list" button refetches from Telegram.
        # On FIRST load (empty list + Telegram configured), auto-fetch.
        if "my_channels" not in st.session_state:
            st.session_state["my_channels"] = []  # list[dict]
        if "channels_auto_loaded" not in st.session_state and config_ok and not st.session_state["my_channels"]:
            st.session_state["channels_auto_loaded"] = True
            with st.spinner("Loading your channels from Telegram…"):
                try:
                    from ingestion.telethon_fetcher import fetch_my_dialogs_sync
                    st.session_state["my_channels"] = fetch_my_dialogs_sync()
                    st.session_state["channels_loaded_at"] = datetime.now(timezone.utc)
                except Exception as e:
                    st.warning(f"Auto-load failed (click Refresh to retry): {e}")

        col_a, col_b = st.columns([3, 1])
        with col_a:
            if st.button(
                "🔄 Refresh channel list",
                disabled=(not config_ok),
                width="stretch",
                help="Pull every channel/group your account has joined from Telegram.",
            ):
                with st.spinner("Loading your channels from Telegram…"):
                    try:
                        from ingestion.telethon_fetcher import fetch_my_dialogs_sync
                        st.session_state["my_channels"] = fetch_my_dialogs_sync()
                        st.session_state["channels_loaded_at"] = datetime.now(timezone.utc)
                    except Exception as e:
                        st.error(f"Could not load channels: {e}")
        with col_b:
            n = len(st.session_state.get("my_channels", []))
            loaded = st.session_state.get("channels_loaded_at")
            if n:
                st.caption(f"{n} channels")
                if loaded:
                    st.caption(loaded.strftime("loaded %H:%M UTC"))

        my_channels = st.session_state.get("my_channels", [])

        channel_ref = ""
        if my_channels:
            # Build human-readable labels and map back to a usable reference.
            def _label(c):
                name = c["title"]
                uname = c.get("username")
                return f"{name} (@{uname})" if uname else f"{name} [id:{c['id']}]"

            labels = [_label(c) for c in my_channels]
            choice = st.selectbox(
                "Select a channel",
                options=range(len(my_channels)),
                format_func=lambda i: labels[i],
                index=0,
                help="Channels/groups your account has joined. Click 'Refresh' to update.",
            )
            sel = my_channels[choice]
            # Prefer @username (stable across runs); fall back to numeric id.
            channel_ref = (
                f"@{sel['username']}" if sel.get("username") else str(sel["id"])
            )
            _selected_channel_title = sel.get("title")
            st.caption(
                f"Type: {sel.get('type', '?')} · "
                f"Ref: `{channel_ref}`"
            )
        else:
            # Fallback: manual entry if the list isn't loaded yet.
            channel_ref = st.text_input(
                "Channel (@username or id)",
                placeholder="@some_channel",
                help="Click 'Refresh channel list' to pick from your joined channels.",
            ).strip()

        start_date = st.date_input(
            "Window start (UTC)",
            value=(datetime.now(timezone.utc).date() - timedelta(weeks=settings.window_weeks)),
        )

        run_btn = st.button(
            "Run backfill",
            disabled=(not channel_ref) or (not config_ok),
            type="primary",
            width="stretch",
        )

        if st.button("Refresh data", width="stretch"):
            st.rerun()

        st.divider()
        st.caption(
            f"DB: `{settings.db_path.name}`\n\n"
            f"GT key: {'set (paid RPM)' if settings.geckoterminal_api_key else 'not set (free RPM)'}"
        )

    # ---- Run action -------------------------------------------------------
    if run_btn and channel_ref.strip():
        title_hint = locals().get("_selected_channel_title")
        run_backfill_action(channel_ref.strip(), start_date, title=title_hint)

    # ---- Tabs -------------------------------------------------------------
    tab_lb, tab_dd, tab_cl = st.tabs(
        ["🏆 Leaderboard", "🔎 Channel Deep-Dive", "📋 Call Log"]
    )
    with tab_lb:
        leaderboard.render()
    with tab_dd:
        channel_deep_dive.render()
    with tab_cl:
        call_log.render()


if __name__ == "__main__":
    main()
