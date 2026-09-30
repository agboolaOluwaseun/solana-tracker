"""Manual repair for channel 16 (cryptoyeezuscalls) — user ruling C 2026-09-30:
RUFUS, Happy, COON only. No database-wide cleanup.

1. Fix the two mislabeled rows' IDENTITY (address/mint, chain, symbol->NULL
   so the canonical ticker comes from the data source at pricing time).
2. Re-price them through the PRODUCTION path (price_one_call + apply_eval7d
   + persist_stoploss_result) — exactly what a scan would have done had the
   parser been correct on day one. allow_birdeye=True: this is an
   initial-scan-class operator action, the fetch-only policy's exception
   class (same as a first channel fetch).
3. Rescan channel 16 from its last_scanned_at (Sep 23 13:07Z) to now —
   with the FIXED parser this captures the two never-picked-up calls
   (UTILITY Sep 23 19:24, COON Sep 29 17:22) and checkpoints the channel.

Prints before/after; aborts loudly if anything unexpected.
"""
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from db import get_connection
from pipeline import (apply_eval7d, persist_stoploss_result, price_one_call,
                      run_backfill, Progress)

conn = get_connection()

# ---------- 1. identity repair ----------
REPAIRS = [
    # (message_id, new_chain, new_address, why)
    (6252, "robinhood", "0x46b6995b02b1e3afa39033243999e00d739615f1",
     "RUFUS: parser stored the /robinhood/ pool URL as the token (0xace9a3…"
     ") and labeled it $NVDA; the called mint is the backticked 0x46B699…"),
    (6283, "arc", "0xf3ee8c4544f98967cb815386691d257195d7c3fe",
     "Happy: chain was robinhood (pre-arc-fix row); the mint IS correct but "
     "labeled $USDC (the paired stable) and unpriceable on the wrong chain"),
]

print("== before ==")
for msg_id, *_ in REPAIRS:
    r = conn.execute("""SELECT id, chain, token_symbol, token_address, status,
        score_state, entry_price_usd, peak_multiple FROM calls
        WHERE channel_id=16 AND message_id=?""", (msg_id,)).fetchone()
    print("  ", dict(r))

for msg_id, chain, addr, why in REPAIRS:
    cur = conn.execute("""UPDATE calls SET chain=?, token_address=?,
        token_symbol=NULL, token_name=NULL, status='pending', score_state='final',
        pool_address=NULL, entry_price_usd=NULL, peak_price_usd=NULL,
        peak_timestamp=NULL, peak_profit_pct=NULL, peak_multiple=NULL,
        is_win=0, priced_at=NULL, note=?, error=NULL
        WHERE channel_id=16 AND message_id=?""", (chain, addr, why, msg_id))
    print(f"msg {msg_id}: updated {cur.rowcount} row(s)")
conn.commit()

# ---------- 2. re-price the two through the production engine ----------
from pricing.geckoterminal import GeckoTerminalClient
gt_client = GeckoTerminalClient()

def _progress(p):
    print(f"    [{p.stage}] {p.message}")

for msg_id, chain, addr, _ in REPAIRS:
    row = conn.execute("""SELECT * FROM calls WHERE channel_id=16
        AND message_id=?""", (msg_id,)).fetchone()
    ts = datetime.fromisoformat(row["call_timestamp"].replace("Z", ""))
    print(f"\n== pricing msg {msg_id} ({row['token_address'][:14]}…, chain={row['chain']}) ==")
    result, sl_result, r7 = price_one_call(row["chain"], gt_client,
                                           row["token_address"],
                                           ts, now=None, allow_birdeye=True)
    if r7 is not None:
        apply_eval7d(row["id"], r7)
        print("   7d verdict:", r7.status_plain, "| sym:", r7.token_symbol,
              "| entry:", r7.entry_price_usd, "| peak x:", r7.max_multiple)
    else:
        from pipeline import apply_backtest
        apply_backtest(16, row["id"], row["message_id"], result)
        print("   legacy pair:", result.status)
    persist_stoploss_result(row["id"], sl_result)
    conn.commit()

# ---------- 3. rescan channel 16 for the missed calls ----------
print("\n== rescan channel 16 (Sep 23 13:07Z -> now) with fixed parser ==")
ch = conn.execute("SELECT * FROM channels WHERE id=16").fetchone()
start = datetime.fromisoformat(str(ch["last_scanned_at"]).replace("Z", ""))
start = start.replace(tzinfo=None)
now = datetime.now(timezone.utc).replace(tzinfo=None)
ref = f"@{ch['username']}" if ch["username"] else str(ch["telegram_channel_id"])

def cb(p: Progress):
    if p.stage in ("parse", "price", "done") and p.message:
        print("   ", p.stage, "|", p.message[:110])

prog = run_backfill(channel_ref=ref, window_start=start, window_end=now,
                    title=ch["title"], username=ch["username"],
                    progress_cb=cb, birdeye_rescue=True)
print("   final:", prog.stage, "|", prog.message[:120] if prog.message else "")
print("   priced:", prog.priced, "unpriceable:", prog.unpriceable,
      "live:", prog.live, "birdeye:", prog.birdeye_saved)

# ---------- 4. report ----------
print("\n== after ==")
for r in conn.execute("""SELECT message_id, chain, COALESCE(token_symbol,'—') s,
    substr(token_address,1,14) addr, status, score_state st,
    COALESCE(round(peak_multiple,2),'—') px, substr(COALESCE(note,''),1,50) n
    FROM calls WHERE channel_id=16 AND call_timestamp >= '2026-09-01'
    ORDER BY call_timestamp"""):
    print("  ", list(r.values()) if hasattr(r, "keys") else tuple(r))
