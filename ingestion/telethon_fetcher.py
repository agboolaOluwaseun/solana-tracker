"""
Telethon-based historical fetcher.

Pulls all messages from a channel over the fixed [window_start, window_end)
window and yields them as RawMessage objects. No live/continuous monitoring —
this is a one-shot lookback driven by the pipeline.

Auth: a one-time interactive login is required the first time a session is
used. Provide TELEGRAM_API_ID / TELEGRAM_API_HASH in .env and, on first run,
enter the phone code Telethon requests. The session file is cached so
subsequent runs are non-interactive.

We run the asyncio loop ourselves via Telethon's .start() pattern, wrapped in
helpers that the (sync) pipeline can call.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import AsyncIterator, List, Optional

from telethon import TelegramClient
from telethon.tl.custom import Message

from config import PROJECT_ROOT, settings
from models import RawMessage

log = logging.getLogger(__name__)


def _session_path() -> str:
    # Telethon appends '.session' itself; pass the stem only.
    return str(PROJECT_ROOT / settings.session_name)


async def _resolve_entity(client: TelegramClient, channel: str):
    """Resolve a channel ref to an entity.

    '@username' resolves directly. A bare numeric string is a Telegram
    channel/group id — Telethon's get_entity would treat it as a username
    and fail, so private channels (no username) must be resolved via
    PeerChannel/PeerChat, which uses the session's entity cache (warmed
    by iter_dialogs in the UI flows).
    """
    if channel.lstrip("-").isdigit():
        from telethon.tl.types import PeerChannel, PeerChat
        peer_id = int(channel)
        try:
            return await client.get_entity(PeerChannel(peer_id))
        except Exception:
            return await client.get_entity(PeerChat(peer_id))
    return await client.get_entity(channel)


def build_client() -> TelegramClient:
    """Construct an un-started TelegramClient."""
    return TelegramClient(
        _session_path(),
        int(settings.telegram_api_id),
        settings.telegram_api_hash,
    )


async def fetch_window(
    channel: str,
    window_start: datetime,
    window_end: datetime,
    limit: Optional[int] = None,
    progress_cb=None,
    opportunistic: bool = False,
) -> tuple[List[RawMessage], Optional[str]]:
    """
    Fetch all messages in [window_start, window_end) for a channel.

    `channel` may be a @username, a t.me link, or a numeric id.
    `limit` caps the number of messages scanned (None = unlimited).
    `progress_cb(count)` is invoked periodically with the running scanned count.
    `opportunistic=True` marks a background refresh fetch: it yields to
    user-initiated fetches — raising TelegramYield before touching (and every
    25 messages during) the shared session instead of blocking them.

    Timestamps are normalised to naive UTC.
    """
    if not settings.telegram_configured:
        raise RuntimeError(
            "Telegram not configured: set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env"
        )

    # One Telethon client on the session file at a time (see telegram_guard).
    # A user fetch blocks until the current holder releases — the lock is
    # taken for the TELETHON FETCH only (seconds per window), never for
    # parsing/pricing, so a refresh can never keep a fetch waiting minutes.
    from ingestion.telegram_guard import TELEGRAM_LOCK, TelegramYield, should_yield_fetch

    if opportunistic and should_yield_fetch():
        raise TelegramYield(channel)
    if not TELEGRAM_LOCK.acquire(timeout=0):
        log.info("waiting for Telegram session (held by another run) — %s", channel)
        while True:
            if opportunistic and should_yield_fetch():
                raise TelegramYield(channel)
            if TELEGRAM_LOCK.acquire(timeout=1.0):
                break

    try:
        client = build_client()
        # Connect without trying to interactively log in — Streamlit has no TTY,
        # so an interactive prompt would just crash. If the session file isn't
        # authorized, raise a clear, actionable error before touching anything.
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError(
                "Telegram session not authorized: no phone number or bot token "
                "available. Run `.venv/bin/python -m scripts.login` from the project "
                "root (in a terminal) to establish the session, then retry."
            )

        out: List[RawMessage] = []
        scanned = 0
        channel_title: Optional[str] = None
        try:
            entity = await _resolve_entity(client, channel)
            # Capture the channel's display title for the leaderboard/UI.
            channel_title = getattr(entity, "title", None)
            # IMPORTANT: do NOT combine offset_date with reverse=True — Telethon's
            # offset_date semantics flip in reverse mode, which silently returns the
            # wrong (tiny) slice. Instead we iterate newest-first (the default) from
            # offset_date=window_end backward, and stop once a message predates
            # window_start. This reliably walks the full [window_start, window_end).
            async for msg in client.iter_messages(
                entity,
                offset_date=window_end,
                limit=limit,
            ):
                scanned += 1
                if opportunistic and scanned % 25 == 0 and should_yield_fetch():
                    # User fetch wants Telegram NOW. Discard this partial
                    # scan (nothing persisted yet) and let the fetch through;
                    # the next refresh redoes this small delta window.
                    log.info("opportunistic fetch yielding at %d scanned — %s",
                             scanned, channel)
                    raise TelegramYield(channel)
                if progress_cb and scanned % 100 == 0:
                    progress_cb(scanned)

                ts = _to_naive_utc(msg.date)
                # We're walking backward in time; once we pass the window start we're done.
                if ts < window_start:
                    break
                # Messages at/after window_end are outside the window (defensive —
                # offset_date should already exclude them).
                if ts >= window_end:
                    continue
                text = (msg.text or "")
                if not text.strip():
                    continue
                out.append(
                    RawMessage(
                        channel_id=_entity_id(entity),
                        message_id=msg.id,
                        text=text,
                        timestamp=ts,
                    )
                )
        finally:
            await client.disconnect()

        if progress_cb:
            progress_cb(scanned)
        log.info("fetched %d messages from %s (scanned %d)", len(out), channel, scanned)
        return out, channel_title
    finally:
        TELEGRAM_LOCK.release()


def fetch_window_sync(
    channel: str,
    window_start: datetime,
    window_end: datetime,
    limit: Optional[int] = None,
    progress_cb=None,
    opportunistic: bool = False,
) -> tuple[List[RawMessage], Optional[str]]:
    """Sync wrapper around fetch_window for use in the (sync) pipeline."""
    return asyncio.run(
        fetch_window(channel, window_start, window_end, limit=limit,
                     progress_cb=progress_cb, opportunistic=opportunistic)
    )


async def fetch_my_dialogs() -> List[dict]:
    """
    List every channel/group the logged-in account is a member of.

    Returns a list of dicts (sorted by title):
        {id, title, username, type}   # type = 'channel' | 'group' | 'megagroup'
    Only channels/groups are returned (1:1 private chats are excluded).
    """
    from ingestion.telegram_guard import TELEGRAM_LOCK
    # User-initiated (opens the dropdown): queue behind whoever holds the
    # session — a dialog list is seconds, so the wait is imperceptible.
    with TELEGRAM_LOCK:
        client = build_client()
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError(
                    "Telegram session not authorized. Run "
                    "`.venv/bin/python -m scripts.login` first."
                )
            out: List[dict] = []
            async for dlg in client.iter_dialogs():
                ent = dlg.entity
                type_str = None
                # Telethon exposes a concrete type per dialog entity.
                cls = type(ent).__name__
                if cls in ("Channel",):
                    # A Channel may be a broadcast channel OR a megagroup.
                    type_str = "megagroup" if getattr(ent, "megagroup", False) else "channel"
                elif cls in ("Chat", "ChatEmpty", "ChatForbidden"):
                    type_str = "group"
                elif cls == "ChannelForbidden":
                    type_str = "channel"
                if type_str is None:
                    continue  # skip User/1:1 chats etc.

                out.append(
                    {
                        "id": ent.id,
                        "title": dlg.title or getattr(ent, "title", None) or f"Chat {ent.id}",
                        "username": getattr(ent, "username", None),
                        "type": type_str,
                    }
                )
        finally:
            await client.disconnect()

    out.sort(key=lambda d: (d["title"] or "").lower())
    return out


def fetch_my_dialogs_sync() -> List[dict]:
    """Sync wrapper around fetch_my_dialogs for the UI."""
    return asyncio.run(fetch_my_dialogs())


# ---- helpers ----------------------------------------------------------------


def _to_naive_utc(dt: datetime) -> datetime:
    """Normalise a (possibly tz-aware) datetime to naive UTC."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _entity_id(entity) -> int:
    """Best-effort channel id for FK linking."""
    return getattr(entity, "id", 0)
