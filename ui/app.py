"""
Streamlit entry point for solana_tracker.

Run with:
    streamlit run ui/app.py

Provides:
  - Sidebar: settings, backfill, re-price, manage data, data info.
  - Four tabs: Leaderboard, Channel Deep-Dive, Call Log, Manage.

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
from db import init_db, get_connection  # noqa: E402
from ui.views import leaderboard, channel_deep_dive, call_log, manage  # noqa: E402

# ──────────────────────────────────────────────────────────────────────────
# Page config
# ──────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Solana Caller Tracker",
    page_icon="🪙",
    layout="wide",
)

# ──────────────────────────────────────────────────────────────────────────
# Dark crypto-dashboard theme (CSS injection)
# ──────────────────────────────────────────────────────────────────────────
_THEME_CSS = """
<style>
/* ── Global ── */
.stApp {
    background: #0a0e17;
}
.block-container {
    padding-top: 2rem;
    max-width: 1400px;
}

/* ── Metric cards ── */
.stMetric {
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 12px;
    padding: 1rem 1.2rem;
    transition: all 0.2s ease;
}
.stMetric:hover {
    border-color: rgba(0,193,159,0.3);
    box-shadow: 0 0 20px rgba(0,193,159,0.08);
}
.stMetric label {
    font-size: 0.75rem !important;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: rgba(255,255,255,0.5) !important;
}
.stMetric [data-testid="stMetricValue"] {
    font-size: 1.75rem !important;
    font-weight: 700;
}

/* ── Sidebar ── */
section[data-testid="stSidebar"] {
    background: #0d1117;
    border-right: 1px solid rgba(255,255,255,0.06);
}
section[data-testid="stSidebar"] .stMarkdown h1,
section[data-testid="stSidebar"] .stMarkdown h2 {
    font-size: 0.85rem !important;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: rgba(255,255,255,0.4) !important;
}

/* ── Tabs ── */
.stTabs [data-baseweb="tab-list"] {
    gap: 6px;
    background: rgba(255,255,255,0.02);
    border-radius: 10px;
    padding: 4px;
}
.stTabs [data-baseweb="tab"] {
    padding: 8px 18px;
    border-radius: 8px;
    font-weight: 500;
    font-size: 0.9rem;
    transition: all 0.2s ease;
}
.stTabs [aria-selected="true"] {
    background: rgba(0,193,159,0.12) !important;
    color: #00C19F !important;
}

/* ── Data tables ── */
.dataframe {
    border-radius: 10px !important;
    overflow: hidden;
    border: 1px solid rgba(255,255,255,0.06) !important;
}
.dataframe th {
    background: rgba(255,255,255,0.04) !important;
    text-transform: uppercase;
    font-size: 0.7rem !important;
    letter-spacing: 0.5px;
}

/* ── Buttons ── */
.stButton button {
    border-radius: 8px;
    font-weight: 500;
    transition: all 0.2s ease;
}
.stButton button:hover {
    transform: translateY(-1px);
    box-shadow: 0 4px 12px rgba(0,0,0,0.3);
}

/* ── Expander ── */
.stExpander {
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 10px;
    overflow: hidden;
}
.stExpander summary {
    font-weight: 500;
}

/* ── Hide noise ── */
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
.stDeployButton {display: none;}

/* ── Scrollbar ── */
::-webkit-scrollbar {width: 8px; height: 8px;}
::-webkit-scrollbar-track {background: transparent;}
::-webkit-scrollbar-thumb {
    background: rgba(255,255,255,0.1);
    border-radius: 4px;
}
::-webkit-scrollbar-thumb:hover {
    background: rgba(255,255,255,0.2);
}

/* ── Hero header ── */
.hero {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-bottom: 0.5rem;
}
.hero-icon {
    font-size: 2.5rem;
    line-height: 1;
}
.hero-title {
    font-size: 1.8rem;
    font-weight: 800;
    background: linear-gradient(135deg, #00C19F 0%, #3B82F6 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    letter-spacing: -0.5px;
}
.hero-sub {
    font-size: 0.85rem;
    color: rgba(255,255,255,0.4);
}

/* ── Status badges ── */
.badge {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 20px;
    font-size: 0.75rem;
    font-weight: 600;
}
.badge-win {background: rgba(0,193,159,0.15); color: #00C19F;}
.badge-loss {background: rgba(239,68,68,0.15); color: #EF4444;}
.badge-pending {background: rgba(251,191,36,0.15); color: #FBBF24;}
.badge-unpriceable {background: rgba(148,163,184,0.15); color: #94A3B8;}

/* ── Section dividers in sidebar ── */
hr {
    border-color: rgba(255,255,255,0.06) !important;
}
</style>
"""


def _hero_html():
    """Render the hero header with gradient title."""
    st.markdown(
        '<div class="hero">'
        '<span class="hero-icon">🪙</span>'
        '<div>'
        '<div class="hero-title">Solana Caller Tracker</div>'
        '<div class="hero-sub">'
        f'Win = peak ≥ {settings.win_multiplier}x within '
        f'{settings.peak_window_hours}h of the call · '
        f'{settings.requests_per_minute} RPM GeckoTerminal'
        '</div>'
        '</div>'
        '</div>',
        unsafe_allow_html=True,
    )


def _db_total_calls() -> int:
    try:
        conn = get_connection()
        row = conn.execute("SELECT COUNT(*) AS n FROM calls").fetchone()
        return row["n"] if row else 0
    except Exception:
        return 0


def _db_total_channels() -> int:
    try:
        conn = get_connection()
        row = conn.execute("SELECT COUNT(*) AS n FROM channels").fetchone()
        return row["n"] if row else 0
    except Exception:
        return 0


def _db_pending_count() -> int:
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM calls WHERE status = 'pending'"
        ).fetchone()
        return row["n"] if row else 0
    except Exception:
        return 0


# ──────────────────────────────────────────────────────────────────────────
# Backfill action (unchanged logic, styled)
# ──────────────────────────────────────────────────────────────────────────
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
            status.update(label=f"📡 Fetching messages… {p.scanned} scanned")
        elif p.stage == "parse":
            status.update(label=f"🔍 Parsing… {p.found} calls found")
        elif p.stage == "price":
            status.update(
                label=(
                    f"💲 Pricing {p.scanned}/{p.total_calls} — "
                    f"✅ {p.priced} priced, ⚫ {p.unpriceable} unpriceable"
                )
            )
        elif p.stage == "done":
            status.update(
                label=(
                    f"✅ Done: {p.found} calls, {p.priced} priced, "
                    f"{p.unpriceable} unpriceable."
                ),
                state="complete",
            )
        elif p.stage == "error":
            msg = p.message or ""
            auth_hints = ("phone number", "bot token", "auth", "UNAUTHORIZED", "2FA")
            if any(h.lower() in msg.lower() for h in auth_hints):
                status.update(
                    label=(
                        "⚠️ Telegram login required. Run in terminal:\n\n"
                        "    .venv/bin/python -m scripts.login\n\n"
                        "Then retry."
                    ),
                    state="error",
                    expanded=True,
                )
            else:
                status.update(label=f"❌ {msg}", state="error", expanded=True)

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
        status.update(label=f"❌ {e}", state="error")
    finally:
        status.update(expanded=False)


# ──────────────────────────────────────────────────────────────────────────
# Re-price action
# ──────────────────────────────────────────────────────────────────────────
def run_reprice_action(channel_id: int, channel_name: str, statuses: list[str]) -> None:
    """Re-price calls for a channel with live progress."""
    from pipeline import reprice_calls, Progress

    label_parts = []
    if "pending" in statuses:
        label_parts.append("pending")
    if "unpriceable_loss" in statuses:
        label_parts.append("unpriceable")
    label = " + ".join(label_parts)

    status = st.status(f"🔄 Re-pricing {label} calls for {channel_name}…", expanded=True)

    def cb(p: Progress):
        if p.stage == "price":
            status.update(label=(
                f"💲 {p.scanned}/{p.total_calls} — "
                f"✅ {p.priced} priced, ⚫ {p.unpriceable} still unpriceable"
            ))
        elif p.stage == "done":
            status.update(label=p.message, state="complete")

    try:
        reprice_calls(channel_id, statuses=statuses, progress_cb=cb)
    except Exception as e:
        status.update(label=f"❌ {e}", state="error")
    finally:
        status.update(expanded=False)
    st.rerun()


# ──────────────────────────────────────────────────────────────────────────
# Delete action
# ──────────────────────────────────────────────────────────────────────────
def run_delete_action(channel_id: int, channel_name: str, delete_channel: bool) -> None:
    """Delete channel data."""
    from pipeline import delete_channel_data

    try:
        result = delete_channel_data(channel_id, delete_channel=delete_channel)
        if delete_channel:
            st.success(
                f"✅ Deleted **{channel_name}**: {result['calls_deleted']} calls, "
                f"{result['runs_deleted']} runs. Channel removed."
            )
        else:
            st.success(
                f"✅ Deleted {result['calls_deleted']} calls and "
                f"{result['runs_deleted']} runs for **{channel_name}**. Channel retained."
            )
        st.rerun()
    except Exception as e:
        st.error(f"❌ Delete failed: {e}")


# ──────────────────────────────────────────────────────────────────────────
# Sidebar
# ──────────────────────────────────────────────────────────────────────────
def _render_sidebar() -> tuple[str | None, object, bool]:
    """Render sidebar, return (channel_ref, start_date, run_btn_clicked)."""
    with st.sidebar:
        # ── Settings ──
        st.markdown("### ⚙️ Settings")
        s1, s2 = st.columns(2)
        s1.metric("Window", f"{settings.window_weeks} weeks")
        s2.metric("Win threshold", f"{settings.win_multiplier}x")
        s1.metric("Peak window", f"{settings.peak_window_hours}h")
        s2.metric("Rate limit", f"{settings.requests_per_minute} RPM")

        st.divider()

        # ── Backfill ──
        st.markdown("### 🚀 Backfill")

        config_ok = settings.telegram_configured
        if not config_ok:
            st.warning("Telegram not configured. Set API_ID / API_HASH in .env")

        if "my_channels" not in st.session_state:
            st.session_state["my_channels"] = []
        if "channels_auto_loaded" not in st.session_state and config_ok and not st.session_state["my_channels"]:
            st.session_state["channels_auto_loaded"] = True
            with st.spinner("Loading channels…"):
                try:
                    from ingestion.telethon_fetcher import fetch_my_dialogs_sync
                    st.session_state["my_channels"] = fetch_my_dialogs_sync()
                    st.session_state["channels_loaded_at"] = datetime.now(timezone.utc)
                except Exception as e:
                    st.warning(f"Auto-load failed: {e}")

        col_a, col_b = st.columns([3, 1])
        with col_a:
            if st.button("🔄 Refresh channels", disabled=(not config_ok), width="stretch"):
                with st.spinner("Loading…"):
                    try:
                        from ingestion.telethon_fetcher import fetch_my_dialogs_sync
                        st.session_state["my_channels"] = fetch_my_dialogs_sync()
                        st.session_state["channels_loaded_at"] = datetime.now(timezone.utc)
                        st.toast("Channels refreshed")
                    except Exception as e:
                        st.error(f"Load failed: {e}")
        with col_b:
            n = len(st.session_state.get("my_channels", []))
            if n:
                st.caption(f"{n} channels")

        my_channels = st.session_state.get("my_channels", [])
        channel_ref = ""
        title_hint = None

        if my_channels:
            def _label(c):
                name = c["title"]
                uname = c.get("username")
                return f"{name} (@{uname})" if uname else f"{name} [id:{c['id']}]"

            labels = [_label(c) for c in my_channels]
            choice = st.selectbox("Channel", options=range(len(my_channels)), format_func=lambda i: labels[i], index=0)
            sel = my_channels[choice]
            channel_ref = f"@{sel['username']}" if sel.get("username") else str(sel["id"])
            title_hint = sel.get("title")
            st.caption(f"Type: {sel.get('type', '?')} · Ref: `{channel_ref}`")
        else:
            channel_ref = st.text_input("Channel (@username or id)", placeholder="@some_channel").strip()

        start_date = st.date_input(
            "Window start (UTC)",
            value=(datetime.now(timezone.utc).date() - timedelta(weeks=settings.window_weeks)),
        )

        run_btn = st.button("🚀 Run backfill", disabled=(not channel_ref) or (not config_ok), type="primary", width="stretch")

        st.divider()

        # ── Re-price ──
        st.markdown("### 🔄 Re-price")
        db_channels = _get_db_channels()
        if db_channels:
            ch_labels = [c["label"] for c in db_channels]
            rp_choice = st.selectbox(
                "Select channel to re-price",
                options=range(len(db_channels)),
                format_func=lambda i: ch_labels[i],
                key="reprice_pick",
            )
            sel_ch = db_channels[rp_choice]
            rp1, rp2 = st.columns(2)
            with rp1:
                if st.button("Re-price pending", key="sb_reprice_pending", width="stretch"):
                    run_reprice_action(sel_ch["id"], sel_ch["label"], ["pending"])
            with rp2:
                if st.button("Re-price unpriceable", key="sb_reprice_unpriceable", width="stretch"):
                    run_reprice_action(sel_ch["id"], sel_ch["label"], ["unpriceable_loss"])
        else:
            st.caption("No channels in DB yet.")

        st.divider()

        # ── Manage Data ──
        st.markdown("### 🗑️ Manage Data")
        if db_channels:
            del_labels = [c["label"] for c in db_channels]
            del_choice = st.selectbox(
                "Select channel to manage",
                options=range(len(db_channels)),
                format_func=lambda i: del_labels[i],
                key="delete_pick",
            )
            sel_del = db_channels[del_choice]
            del_confirm = st.checkbox("Confirm deletion", key="sb_del_confirm")
            d1, d2 = st.columns(2)
            with d1:
                if st.button("Delete calls", key="sb_del_calls", disabled=(not del_confirm), width="stretch"):
                    run_delete_action(sel_del["id"], sel_del["label"], delete_channel=False)
            with d2:
                if st.button("Delete all", key="sb_del_all", disabled=(not del_confirm), width="stretch"):
                    run_delete_action(sel_del["id"], sel_del["label"], delete_channel=True)
        else:
            st.caption("No channels to manage.")

        st.divider()

        # ── Data Info ──
        st.markdown("### 📊 Data Info")
        st.caption(f"DB: `{settings.db_path.name}`")
        st.caption(f"GT key: {'✅ set' if settings.geckoterminal_api_key else '❌ not set'}")
        st.caption(f"Channels: {_db_total_channels()} · Calls: {_db_total_calls()}")
        pending_n = _db_pending_count()
        if pending_n > 0:
            st.warning(f"⚠️ {pending_n} calls are pending (un-priced)")

    return channel_ref, start_date, run_btn, title_hint


def _get_db_channels() -> list[dict]:
    """Return list of {id, label} for channels in the DB."""
    try:
        conn = get_connection()
        rows = conn.execute("SELECT id, title, username FROM channels ORDER BY title").fetchall()
        return [
            {"id": r["id"], "label": r["title"] or r["username"] or f"Channel {r['id']}"}
            for r in rows
        ]
    except Exception:
        return []


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────
def main() -> None:
    init_db()
    st.markdown(_THEME_CSS, unsafe_allow_html=True)
    _hero_html()

    channel_ref, start_date, run_btn, title_hint = _render_sidebar()

    if run_btn and channel_ref and channel_ref.strip():
        run_backfill_action(channel_ref.strip(), start_date, title=title_hint)

    # ── Tabs ──
    tab_lb, tab_dd, tab_cl, tab_mg = st.tabs([
        "🏆 Leaderboard",
        "🔎 Deep-Dive",
        "📋 Call Log",
        "⚙️ Manage",
    ])
    with tab_lb:
        leaderboard.render()
    with tab_dd:
        channel_deep_dive.render()
    with tab_cl:
        call_log.render()
    with tab_mg:
        manage.render()


if __name__ == "__main__":
    main()
