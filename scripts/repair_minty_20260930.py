"""MintyFreshCalls (ch55) repair — 5th channel of the audit program.

One genuine defect (RUFUS-class victim, era before mint-beats-link):
  msg 12120 stored the dexscreener POOL id (0x5cebb…64hex, the EULER-musebook
  PAIR) as the token. The message's in-text MINT is 0x434d… — the real EULER.
  Repoint + reprice via production path (same surgery as DECENTRAL/Rick).

NOT touched (correct as-is / policy for user):
  msg 12112 struck-out ~~0x82e4~~ + rotation 0x434d: strikethrough-update post.
    0x82e4 row (original call, abandoned) stays as the tracker's first-mention
    record; strikeout-skip semantics = watchlist class, proposed, not invented.
  msg 11955: robinscan giveaway-wallet post — not a token call. Correct skip.
  msg 11992: gmgn /address/ copy-trading wallet link — wallet, not mint.
    Correct skip (mirror of debank policy).
"""
import sys
from datetime import datetime

sys.path.insert(0, ".")

from db import get_connection, transaction
from pipeline import price_one_call, apply_eval7d, persist_stoploss_result

conn = get_connection()
row = conn.execute("""SELECT id, chain, message_id, token_address, call_timestamp
    FROM calls WHERE channel_id=55 AND message_id=12120""").fetchone()
assert row, "msg 12120 row vanished (boot may have changed things — re-audit)"
print("before:", dict(row))
assert row["token_address"] == (
    "0x5cebbedc2f2c6ceccf01db6ce71ceb399c940e5d44ee9f2213ffa83dab6809dc"), \
    "row already repointed/changed elsewhere — aborting for re-check"

# verify no row exists for the true mint in ch55 (else it'd be a dup)
MINT = "0x434d49b8e2c905e133d28060fd38cb8b8e9b4ba3"
dup = conn.execute("""SELECT id, status FROM calls WHERE channel_id=55
    AND token_address=?""", (MINT,)).fetchone()
assert not dup, f"a 0x434d row already exists ({dict(dup)}) — different story, abort"

with transaction() as c:
    c.execute("""UPDATE calls SET token_address=?, token_symbol=NULL,
        status='pending', score_state='final', pool_address=NULL,
        entry_price_usd=NULL, peak_price_usd=NULL, peak_multiple=NULL,
        peak_profit_pct=NULL, peak_timestamp=NULL, is_win=0, priced_at=NULL,
        note='repaired: dexscreener pool id was stored as the token; repointed to the in-text EULER mint (RUFUS-class, audit 2026-09-30)',
        error=NULL WHERE id=?""", (MINT, row["id"]))
print("repointed to", MINT[:14], "— repricing via production engine")

from pricing.geckoterminal import GeckoTerminalClient
ts = datetime.fromisoformat(str(row["call_timestamp"]).replace("Z", ""))
res, sl, r7 = price_one_call(row["chain"], GeckoTerminalClient(), MINT, ts,
                             now=None, allow_birdeye=True)
if r7 is not None:
    _eff = apply_eval7d(row["id"], r7)
    print("verdict:", r7.status_plain, "| sym:", r7.token_symbol,
          "| entry:", r7.entry_price_usd, "| peak:",
          round(r7.max_multiple or 0, 2), "x | note:", r7.note[:50])
else:
    _eff = row["id"]
    from pipeline import apply_backtest
    apply_backtest(55, row["id"], row["message_id"], res)
    print("legacy:", res.status)
persist_stoploss_result(_eff, sl)
conn.commit()

print("\nafter:", dict(conn.execute("""SELECT message_id, substr(token_address,1,18) a,
    COALESCE(token_symbol,'-') s, status, round(peak_multiple,2) m,
    substr(COALESCE(note,''),1,40) n FROM calls WHERE channel_id=55
    AND message_id=12120""").fetchone()))
print("trail:", conn.execute("""SELECT status FROM trailing_results WHERE call_id=?""",
                             (row["id"],)).fetchone())
