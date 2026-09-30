"""Casino Eye repair — surgical (user: don't break what works).

The fixed parser recovers 6 calls the gmgn blind spot lost in Sept:
  EVM/robinhood: 2681, 2682, 2683 ($PAR x300+), 2703, 2705
  SOL:           2710 (eye1_ prefix, gmgn.ai/sol/token)
The 4 existing rows are untouched. Insert pending rows with the same
shape the scan would have made them, then price via the production path
(price_one_call, allow_birdeye=True — scan-class operator action)."""
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from db import get_connection, transaction
from pipeline import (price_one_call, apply_eval7d, persist_stoploss_result,
                      make_buckets, _iso, Progress)
from pricing.geckoterminal import GeckoTerminalClient

RECOVER = [  # (message_id, chain, token_address, timestamp)
    (2681, "robinhood", "0xe3a7f023a4aa2a8e4123232c3de8ed5691d382da", "2026-09-02T20:40:16Z"),
    (2682, "robinhood", "0x56a0c0d25ec1c9baf65d5202d7fe28d40b64652a", "2026-09-03T11:47:44Z"),
    (2683, "robinhood", "0x507b6f349a80114097a67b8b4677367acc15b220", "2026-09-04T20:13:56Z"),
    (2703, "robinhood", "0x5e1e8a9ca276fa179f657fc92ca7303824c05eab", "2026-09-10T16:51:30Z"),
    (2705, "robinhood", "0x365529d211e23dc2a87637438231120d23c5f47a", "2026-09-11T15:35:22Z"),
    (2710, "sol",       "5wn857GFhKHA6f2dcFG8AQEzCD96DkZpiviV6csWD8vj", "2026-09-24T22:55:06Z"),
]

conn = get_connection()

# verify timestamps/messages against the raw table copy? (no — DB has none;
# these came from the Telethon replay, re-read the msgs live to confirm+raw)
import asyncio
from ingestion.telethon_fetcher import build_client

async def fetch_raw():
    client = build_client(); await client.connect()
    ent = await client.get_entity("thecasinoeye")
    out = {}
    for mid, *_ in RECOVER:
        m = await client.get_messages(ent, ids=[mid])
        m = m[0] if isinstance(m, list) else m
        out[mid] = (m.text or "", m.date.astimezone(timezone.utc).replace(tzinfo=None))
    await client.disconnect()
    return out

raws = asyncio.run(fetch_raw())

buckets = make_buckets(datetime.fromisoformat(
    conn.execute("SELECT window_start FROM channels WHERE id=62")
    .fetchone()["window_start"].replace("Z", "")))

print("== insert recovered calls (scan shape, idempotent) ==")
for mid, chain, addr, _ts in RECOVER:
    text, ts = raws[mid]
    week = buckets.week_index(ts) or 1
    raw = text[:2000]
    with transaction() as c:
        c.execute("""INSERT OR IGNORE INTO calls
            (channel_id, chain, message_id, raw_text, token_address,
             token_symbol, token_name, call_timestamp, week_index, status)
            VALUES (62,?,?,?,?,?,?,?,?,'pending')""",
            (chain, mid, raw, addr, None, None, _iso(ts), week))
    r = conn.execute("""SELECT id, status, score_state FROM calls
        WHERE channel_id=62 AND message_id=? AND token_address=?""",
        (mid, addr)).fetchone()
    print(f"  msg {mid} -> call {r['id']} [{r['status']}/{r['score_state']}] chain={chain}")

print("\n== price each via production engine (birdeye-eligible) ==")
client = GeckoTerminalClient()
for mid, chain, addr, _ in RECOVER:
    r = conn.execute("""SELECT id, call_timestamp FROM calls WHERE channel_id=62
        AND message_id=? AND token_address=?""", (mid, addr)).fetchone()
    call_id, ts_raw = r["id"], r["call_timestamp"]
    ts = datetime.fromisoformat(str(ts_raw).replace("Z", ""))
    res, sl, r7 = price_one_call(chain, client, addr, ts, now=None,
                                 allow_birdeye=True)
    if r7 is not None:
        apply_eval7d(call_id, r7)
        print(f"  msg {mid} {addr[:14]}…: {r7.status_plain}"
              f" entry={r7.entry_price_usd} peak={round(r7.max_multiple or 0,2)}x"
              f" sym={r7.token_symbol} birdeye={'birdeye' in (r7.note or '')}")
    else:
        from pipeline import apply_backtest
        apply_backtest(62, call_id, mid, res)
        print(f"  msg {mid}: legacy {res.status}")
    persist_stoploss_result(call_id, sl)

# checkpoint so the next boot doesn't re-do work it can skip (dedup-safe
# anyway via INSERT OR IGNORE, but this matches scan behaviour)
now = datetime.now(timezone.utc).replace(tzinfo=None)
with transaction() as c:
    c.execute("UPDATE channels SET last_scanned_at=?, window_end=? WHERE id=62",
              (_iso(now), _iso(now)))

print("\n== final Sept roster for The Casino ==")
for r in conn.execute("""SELECT message_id, chain, COALESCE(token_symbol,'—') s,
    substr(token_address,1,14), status, score_state, round(peak_multiple,1),
    substr(COALESCE(note,''),1,34) n FROM calls
    WHERE channel_id=62 AND call_timestamp>='2026-09-01' ORDER BY call_timestamp"""):
    print("  ", list(r.values()))
print("\nsummary:", conn.execute("""SELECT COUNT(*), SUM(status='win'), SUM(status='loss'),
    SUM(status='unpriceable_loss'), SUM(score_state='live') FROM calls
    WHERE channel_id=62 AND call_timestamp>='2026-09-01'""").fetchone())
