"""tomleessonscalls (ch58) repair — 4th channel of the audit program.

VERDICTS from the ground-truth replay (145 Sept messages, raw-regex pass):
RECOVER 4 (real calls, engine-blind when they posted):
  6354 gmgn.ai/eth/token/0x5c67b6dc…   (Sept 7 — gmgn hosts unknown then)
  6361 gmgn.ai/eth/token/0xf12cbb0b…   (Sept 10 — same)
  6411 gmgn.ai/arc/token/0xc468a711…   (Sept 15 — in-text mint present now)
  6412 gmgn.ai/arc/token/0x6d4ac873…   (Sept 15 — same)
  All four: asserted the CURRENT parser captures before inserting (proof
  the class is dead; these are victims of the pre-fix era).
RETRY 1 unpriceable with a fresh DS probe: 6429 bare mint 'no pool'
  (6404 defined.fi token lives on the INK chain — unsupported by policy,
   honest unpriceable stays).
DELETE 2 garbage rows (classes already fixed in the engine):
  6348: Proficy bot blast — old parser stored the dexscreener POOL id
        (0x9adf…, 64-hex) as the "token"; the REAL call (OTER, 0x7e68…)
        is msg 6347 same minute, already recorded. RUFUS-class victim,
        duplicate artifact.
  6459: 'GENERATIONALLL…' hype post — the extractor sliced the run of L's
        tail ('NALLLL…', 44 chars, 3 unique chars) as a Solana mint. No
        real address in the message. unpriceable artifact.
NOT touched: 6468 crystal.exchange launchpad (new-launchpad post, pre-trade
  — presale-class, parser-correct to skip; flag for policy).
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

RECOVER = [
    (6354, "0x5c67b6dc46bbc8b16c7e72749e57138d22b9b3b2"),
    (6361, "0xf12cbb0b9a0bcc6fc6782775f513bee049516ac9"),
    (6411, "0xc468a7117725722c163cef0f717414e0a7afeaa9"),
    (6412, "0x6d4ac8738dc285b4a7e03848f4c1a95d54d9785f"),
]
RETRY = [(6429, "robinhood", "0x4f53e7a5eee3b53c4e6aadd112637e0607860087")]
DELETE = [6348, 6459]

conn = get_connection()

# ---- Telethon: real texts (also for insert raw_text) ----
async def fetch_raw(mids):
    from ingestion.telethon_fetcher import build_client
    client = build_client(); await client.connect()
    ent = await client.get_entity("tomleessonscalls")
    out = {}
    for mid in mids:
        m = await client.get_messages(ent, ids=[mid])
        m = m[0] if isinstance(m, list) else m
        out[mid] = (m.text or "", m.date.astimezone(timezone.utc).replace(tzinfo=None))
    await client.disconnect()
    return out

texts = asyncio.run(fetch_raw([m for m, _ in RECOVER] + [m for m, _, _ in RETRY]))

# ---- assert current parser captures each recovery ----
from chains.chain_detector import detect_chain
from pipeline import apply_backtest
for mid, addr in RECOVER:
    text, ts = texts[mid]
    ep = [x.token_address for x in rh.parse_calls(58, mid, text, ts)]
    assert addr in ep, f"msg {mid}: current parser does NOT capture {addr}"
    sp = parse_message(RawMessage(channel_id=58, message_id=mid, text=text, timestamp=ts))
    assert sp is None, f"msg {mid}: solana parser fired too — ambiguous, abort"
    print(f"msg {mid}: current parser captures ✓ {addr[:14]}…  chain={detect_chain(text, addr)}")

buckets = make_buckets(datetime.fromisoformat(
    conn.execute("SELECT window_start FROM channels WHERE id=58")
    .fetchone()["window_start"].replace("Z", "")))

client = GeckoTerminalClient()

print("\n== insert + price recoveries ==")
for mid, addr in RECOVER:
    text, ts = texts[mid]
    chain = detect_chain(text, addr)
    week = buckets.week_index(ts) or 1
    with transaction() as c:
        c.execute("""INSERT OR IGNORE INTO calls
            (channel_id, chain, message_id, raw_text, token_address,
             token_symbol, token_name, call_timestamp, week_index, status)
            VALUES (58,?,?,?,?,?,?,?,?,'pending')""",
            (chain, mid, text[:2000], addr, None, None, _iso(ts), week))
    row = conn.execute("""SELECT id, call_timestamp FROM calls WHERE channel_id=58
        AND message_id=? AND token_address=?""", (mid, addr)).fetchone()
    tts = datetime.fromisoformat(str(row["call_timestamp"]).replace("Z", ""))
    res, sl, r7 = price_one_call(chain, client, addr, tts, now=None,
                                 allow_birdeye=True)
    if r7 is not None:
        apply_eval7d(row["id"], r7)
        print(f"  msg {mid} [{chain}] {addr[:12]}…: {r7.status_plain} "
              f"sym={r7.token_symbol!r} peak={round(r7.max_multiple or 0,2)}x "
              f"birdeye={'birdeye' in (r7.note or '')}")
    else:
        apply_backtest(58, row["id"], mid, res)
        print(f"  msg {mid}: legacy {res.status}")
    persist_stoploss_result(row["id"], sl)

print("\n== retry unpriceable 6429 ==")
for mid, chain, addr in RETRY:
    row = conn.execute("""SELECT id, call_timestamp FROM calls WHERE channel_id=58
        AND message_id=?""", (mid,)).fetchone()
    tts = datetime.fromisoformat(str(row["call_timestamp"]).replace("Z", ""))
    res, sl, r7 = price_one_call(chain, client, addr, tts, now=None,
                                 allow_birdeye=True)
    if r7 is not None and r7.status_plain != "unpriceable_loss":
        apply_eval7d(row["id"], r7)
        persist_stoploss_result(row["id"], sl)
        print(f"  6429 now priced: {r7.status_plain} sym={r7.token_symbol!r}")
    else:
        print("  6429 still unpriceable (honest — GT/DS/Birdeye all blind)")

print("\n== delete garbage rows (classes already dead in the engine) ==")
for mid in DELETE:
    row = conn.execute("""SELECT id, token_address, status FROM calls
        WHERE channel_id=58 AND message_id=?""", (mid,)).fetchone()
    if not row:
        print(f"  msg {mid}: no row (already absent)"); continue
    cid = row["id"]
    with transaction() as c:
        c.execute("DELETE FROM trailing_results WHERE call_id=?", (cid,))
        c.execute("DELETE FROM stoploss_results WHERE call_id=?", (cid,))
        c.execute("DELETE FROM calls WHERE id=?", (cid,))
    print(f"  msg {mid}: deleted call {cid} ({row['status']}, {(row['token_address'] or '')[:14]}…)")

conn.commit()
print("\n== after: Sept totals ==")
print(conn.execute("""SELECT COUNT(*), SUM(status='win'), SUM(status='loss'),
    SUM(status NOT IN ('win','loss')) FROM calls
    WHERE channel_id=58 AND call_timestamp>='2026-08-31T23:00'""").fetchone())
for r in conn.execute("""SELECT message_id, chain, COALESCE(token_symbol,'—'),
    substr(token_address,1,12), status, round(peak_multiple,1),
    substr(COALESCE(note,''),1,26) FROM calls WHERE channel_id=58
    AND message_id IN (6354,6361,6411,6412,6429) ORDER BY message_id"""):
    print("  ", tuple(r))
