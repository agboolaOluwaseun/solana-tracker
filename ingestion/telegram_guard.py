"""Cross-run Telegram access guard.

Two pipeline runs (a background channel refresh and a user-initiated 5-month
fetch) may run concurrently for everything EXCEPT Telegram: Telethon sessions
are single-writer SQLite files, and two live clients on one file risk auth
corruption / FloodWait races.

Rules implemented here (user decision 2026-09-16: 'fetch starts immediately,
not waiting at all for anything'):

* every Telethon window fetch holds TELEGRAM_LOCK for its duration, so two
  fetches never touch the session file at once (each is seconds-long; the
  wait is imperceptible);
* a user fetch raises the YIELD flag at stream start; opportunistic
  (refresh) fetches check it per message-batch and return their PARTIAL
  result immediately instead of blocking — partial is safe because the call
  ledger is append-only: the next refresh re-fetches the delta from the
  channel's last stored call timestamp;
* the flag is cleared when the user fetch stream ends, so the next refresh
  (e.g. the frontend's auto-resume) completes normally.
"""
from __future__ import annotations

import threading

TELEGRAM_LOCK = threading.Lock()
YIELD_TO_USER_FETCH = threading.Event()


class TelegramYield(Exception):
    """Raised by an opportunistic (refresh) fetch when a user-initiated
    fetch is waiting for / holding the Telegram session. The refresh run
    returns early WITHOUT persisting anything for this channel, so the next
    refresh re-does the (small) delta window from the same start point —
    append-only ledger semantics make partial discards safe."""


def request_telegram_yield(on: bool = True) -> None:
    if on:
        YIELD_TO_USER_FETCH.set()
    else:
        YIELD_TO_USER_FETCH.clear()


def should_yield_fetch() -> bool:
    return YIELD_TO_USER_FETCH.is_set()
