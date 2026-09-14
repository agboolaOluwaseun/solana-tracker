"""Download Telegram profile photos for every channel that lacks one.

Cards render /channel_photos/<db_id>.jpg; channels created after the initial
manual batch (12-19) never got a file, so they show the initials fallback.
This resolves each channel (by @username, falling back to the numeric
Telegram id via the session's entity cache) and downloads its current
profile photo. Re-run any time; existing files are skipped unless --force.

Uses the same authorized Telethon session as the pipeline (no interactive
prompt — errors clearly if not logged in).
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PHOTO_DIR = ROOT / "frontend" / "public" / "channel_photos"


async def _download(force: bool) -> int:
    from db import get_connection
    from ingestion.telethon_fetcher import build_client, _resolve_entity

    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, username, telegram_channel_id, title FROM channels ORDER BY id"
    ).fetchall()
    todo = [r for r in rows
            if force or not (PHOTO_DIR / f"{r['id']}.jpg").exists()]
    if not todo:
        print("every channel already has a photo")
        return 0
    print(f"{len(todo)} channel(s) missing photos")

    client = build_client()
    await client.connect()
    if not await client.is_user_authorized():
        print("ERROR: Telegram session not authorized — run scripts/login first")
        return 1

    # Warm the entity cache (needed to resolve private channels by numeric id).
    try:
        async for _ in client.iter_dialogs():
            pass
    except Exception:
        pass

    ok = failed = none = 0
    for r in todo:
        ref = f"@{r['username']}" if r["username"] else str(r["telegram_channel_id"])
        out = PHOTO_DIR / f"{r['id']}.jpg"
        try:
            entity = await _resolve_entity(client, ref)  # type: ignore[arg-type]
            # Pass an extension-less stem: Telethon appends the real extension
            # (jpeg/png) and returns the final path — rename it to <id>.jpg so
            # the frontend's /channel_photos/<id>.jpg URL always resolves.
            stem = str(PHOTO_DIR / f"_{r['id']}_tmp")
            path = await client.download_profile_photo(
                entity, file=stem, download_big=True)
            if path and Path(path).exists():
                os.replace(path, out)
                ok += 1
                print(f"  ✓ {r['title']} → {out.name}")
            else:
                none += 1
                print(f"  – {r['title']} has no profile photo (kept initials)")
        except Exception as e:
            failed += 1
            print(f"  ✗ {r['title']}: {type(e).__name__}: {e}")
    await client.disconnect()
    print(f"done: {ok} downloaded, {none} without photo, {failed} failed")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="re-download even if a file exists (refreshes all)")
    sys.exit(asyncio.run(_download(ap.parse_args().force)))
