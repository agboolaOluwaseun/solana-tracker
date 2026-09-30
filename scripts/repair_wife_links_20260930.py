"""wifechangingcallss (ch41) — recover the 3 calls lost to the dexscreener
cross-chain link blindness, now fixed in engine (1b37a27). Same pattern as
scripts/repair_wife_20260930.py: assert CURRENT parser captures, insert scan
shape, price via production path.

  16437  09-08  dexscreener.com/bsc/0x3eA3…7553d   (in-text? no: link-only)
  16845  09-18  dexscreener.com/bsc/0x1f12…217Ba   (link-only, burnt-supply prose)
  17101  09-28  dexscreener.com/base/0xbedf…a0a5   (pool64 link -> SPIKE(base))
"""
import asyncio
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from db import get_connection, transaction
from pipeline import (price_one_call, apply_eval7d, persist_stoploss_result,
                      make_buckets, _iso)
from models import RawMessage
from pricing.geckoterminal import GeckoTerminalClient
from ingestion.address_parser import parse_message
from chains.robinhood_impl import parser as rh
from chains.chain_detector import detect_chain

RECOVER = [
    (16437, "0x3ea3f9a7b7edbfe0ba3568bfc0f30ba870b7553d"),
    (16845, "0x1f12111deb29e8ae246aa53f17839fe778a217ba"),
    (17101, "0xbedfdb1c91ccfd3269d5aec696c95e96457205ab9cab1ebe6be608e295a3a0a5"),
]

conn = get_connection()

async def fetch_raw():
    from ingestion.telethon_fetcher import build_client
    client = build_client(); await client.connect()
    ent = await client.get_entity("wifechangingcallss")
    out = {}
    for mid, _ in RECOVER:
        m = await client.get_messages(ent, ids=[mid])
        m = m[0] if isinstance(m, list) else m
        out[mid] = (m.text or "", m.date.astimezone(timezone.utc).replace(tzinfo=None))
    await client.disconnect()
    return out

texts = asyncio.run(fetch_raw())

for mid, addr in RECOVER:
    text, ts = texts[mid]
    ep = [x.token_address for x in rh.parse_calls(41, mid, text, ts)]
    assert addr in ep, f"msg {mid}: parser blind even after fix ({ep})"
    assert detect_chain(text, addr) in ("bsc", "base"), f"msg {mid}: chain misdetected"
    sp = parse_message(RawMessage(channel_id=41, message_id=mid, text=text, timestamp=ts))
    assert sp is None, f"msg {mid}: solana parser fired too"
    print(f"msg {mid}: captured ✓  chain={detect_chain(text, addr)}  {addr[:14]}…")

buckets = make_buckets(datetime.fromisoformat(
    conn.execute("SELECT window_start FROM channels WHERE id=41")
    .fetchone()["window_start"].replace("Z", "")))

client = GeckoTerminalClient()
print("\n== insert + price ==")
for mid, addr in RECOVER:
    text, ts = texts[mid]
    chain = detect_chain(text, addr)
    week = buckets.week_index(ts) or 1
    with transaction() as c:
        c.execute("""INSERT OR IGNORE INTO calls
            (channel_id, chain, message_id, raw_text, token_address,
             token_symbol, token_name, call_timestamp, week_index, status)
            VALUES (41,?,?,?,?,?,?,?,?,'pending')""",
            (chain, mid, text[:2000], addr, None, None, _iso(ts), week))
    row = conn.execute("""SELECT id, call_timestamp FROM calls WHERE channel_id=41
        AND message_id=? AND token_address=?""", (mid, addr)).fetchone()
    tts = datetime.fromisoformat(str(row["call_timestamp"]).replace("Z", ""))
    res, sl, r7 = price_one_call(chain, client, addr, tts, now=None,
                                 allow_birdeye=True)
    if r7 is not None:
        apply_eval7d(row["id"], r7)
        print(f"  msg {mid} [{chain}]: {r7.status_plain} sym={r7.token_symbol!r} "
              f"peak={round(r7.max_multiple or 0,2)}x birdeye={'birdeye' in (r7.note or '')}")
    else:
        from pipeline import apply_backtest
        apply_backtest(41, row["id"], mid, res)
        print(f"  msg {mid}: legacy {res.status}")
    persist_stoploss_result(row["id"], sl)

print("\n== after ==")
for r in conn.execute("""SELECT message_id, chain, COALESCE(token_symbol,'—'),
    status, score_state, round(peak_multiple,1) FROM calls WHERE channel_id=41
    AND message_id IN (16437,16845,17101)"""):
    print("  ", tuple(r))
print(conn.execute("""SELECT COUNT(*), SUM(status='win'), SUM(status='loss'),
    SUM(status NOT IN ('win','loss')) FROM calls
    WHERE channel_id=41 AND call_timestamp>='2026-08-31T23:00'""").fetchone())
