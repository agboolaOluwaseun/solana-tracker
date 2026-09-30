"""wifechangingcallss repair — 5 genuinely-lost Sept calls (gmgn-adjacent
audit, 2026-09-30). Ground truth: raw-regex Telethon replay (case-normalized,
cross-window aware). Recovered = bare in-text mints the OLD scan missed but
the CURRENT parser provably captures (round-2 replay confirms). Same pattern
as scripts/repair_casinoeye_20260930.py."""
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

RECOVER = [  # (message_id, chain_guess, token_address, Sept date)
    (16262, "robinhood", "0xe3a7f023a4aa2a8e4123232c3de8ed5691d382da", "MEOW"),
    (16285, "robinhood", "0x93f6fe291da8028604efd75b965df3656e7ffc61", "ROBINCAT#2"),
    (16640, "robinhood", "0x3a6215b2c790169c2928e8f823ea8ddba1155a56", "DOGE(rh)"),
    (16838, "robinhood", "0xe58b505dfa30a2c64f6447ead0f6009419161e18", "?"),
    (16950, "robinhood", "0xef67e3064bef1a27e81925ec7132f23e533bd5f6", "GME(rh)"),
]

conn = get_connection()

# sanity: current parser must capture each before we trust a recovery
async def fetch_raw():
    from ingestion.telethon_fetcher import build_client
    client = build_client(); await client.connect()
    ent = await client.get_entity("wifechangingcallss")
    out = {}
    for mid, *_ in RECOVER:
        m = await client.get_messages(ent, ids=[mid])
        m = m[0] if isinstance(m, list) else m
        out[mid] = (m.text or "", m.date.astimezone(timezone.utc).replace(tzinfo=None))
    await client.disconnect()
    return out

raws = asyncio.run(fetch_raw())
for mid, chain, addr, tag in RECOVER:
    text, ts = raws[mid]
    ep = [x.token_address for x in rh.parse_calls(41, mid, text, ts)]
    sp = parse_message(RawMessage(channel_id=41, message_id=mid, text=text, timestamp=ts))
    assert addr in ep, f"msg {mid}: current EVM parser does NOT capture {addr} ({ep})"
    assert sp is None, f"msg {mid}: solana parser also fired — ambiguous"
    print(f"msg {mid} ({tag}) {ts:%m-%d %H:%M}: current parser captures ✓  {addr[:16]}…")

buckets = make_buckets(datetime.fromisoformat(
    conn.execute("SELECT window_start FROM channels WHERE id=41")
    .fetchone()["window_start"].replace("Z", "")))

print("\n== insert + price via production ==")
client = GeckoTerminalClient()
for mid, chain, addr, tag in RECOVER:
    text, ts = raws[mid]
    week = buckets.week_index(ts) or 1
    with transaction() as c:
        c.execute("""INSERT OR IGNORE INTO calls
            (channel_id, chain, message_id, raw_text, token_address,
             token_symbol, token_name, call_timestamp, week_index, status)
            VALUES (41,?,?,?,?,?,?,?,?,'pending')""",
            (chain, mid, text[:2000], addr, None, None, _iso(ts), week))
    row = conn.execute("""SELECT id, call_timestamp FROM calls WHERE channel_id=41
        AND message_id=? AND token_address=?""", (mid, addr)).fetchone()
    call_id, ts_raw = row["id"], row["call_timestamp"]
    tts = datetime.fromisoformat(str(ts_raw).replace("Z", ""))
    res, sl, r7 = price_one_call(chain, client, addr, tts, now=None,
                                 allow_birdeye=True)
    if r7 is not None:
        apply_eval7d(call_id, r7)
        print(f"  msg {mid} {tag:10}: {r7.status_plain:8} sym={r7.token_symbol!r} "
              f"entry={r7.entry_price_usd} peak={round(r7.max_multiple or 0,2)}x "
              f"birdeye={'birdeye' in (r7.note or '')}")
    else:
        from pipeline import apply_backtest
        apply_backtest(41, call_id, mid, res)
        print(f"  msg {mid} {tag}: legacy {res.status}")
    persist_stoploss_result(call_id, sl)

print("\n== after: Sept roster summary ==")
print(conn.execute("""SELECT COUNT(*), SUM(status='win'), SUM(status='loss'),
    SUM(status NOT IN ('win','loss')) FROM calls
    WHERE channel_id=41 AND call_timestamp>='2026-08-31T23:00'""").fetchone())
for r in conn.execute("""SELECT message_id, chain, COALESCE(token_symbol,'—') s,
    substr(token_address,1,12), status, round(peak_multiple,1),
    substr(COALESCE(note,''),1,30) FROM calls WHERE channel_id=41
    AND message_id IN (16262,16285,16640,16838,16950)"""):
    print("  ", tuple(r))
