"""
One-time interactive Telegram login.

Run this from the TERMINAL (not the Streamlit UI) to establish or refresh the
Telethon session. Streamlit can't do interactive phone/code prompts, so the
session must be created here first. After it succeeds, backfills run from the
UI non-interactively.

Usage (from the project root):
    .venv/bin/python -m scripts.login
    .venv/bin/python -m scripts.login --phone +15551234567

If the copied session is still valid, this logs in instantly with no prompts.
Otherwise it asks for phone -> login code -> (2FA password if enabled).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from ingestion.telethon_fetcher import build_client  # noqa: E402


async def main(phone: str | None) -> int:
    if not settings.telegram_configured:
        print("ERROR: set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first.")
        return 1

    client = build_client()
    print(f"Session file : {settings.session_name}.session")
    print("Connecting... (enter phone / code / 2FA password if prompted)\n")

    # If a phone is supplied use it; otherwise let Telethon prompt via input().
    # When the session is already authorized, none of these prompts fire.
    phone_val = phone or settings.telegram_phone or (
        lambda: input("Enter your phone number (intl format, e.g. +15551234567): ")
    )

    try:
        await client.start(phone=phone_val)
    except Exception as e:
        print(f"\nLogin failed: {e}")
        await client.disconnect()
        return 1

    me = await client.get_me()
    name = (me.first_name or "") + (f" {me.last_name}" if me.last_name else "")
    print(f"\n✓ Authorized as: {name.strip()} (@{me.username}) [id={me.id}]")
    await client.disconnect()
    print("✓ Session saved. You can now run backfills from the Streamlit UI.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Establish the Telegram session.")
    ap.add_argument("--phone", default=None, help="Phone in intl format, e.g. +15551234567")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.phone)))
