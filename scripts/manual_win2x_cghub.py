"""Manual user-directed overrides (2026-09-24), Cryptogem Hub (channel 64):

  2791 museic | 2728 DRUGS/Big Pharmai | 2731 LIQ | 2716 SCOUT
  2750 0x16ee43… | 2759 0x69d62e… | 2711 🟥🟩

  • Normal strategy -> WIN at 2.0x (user verdict; no investigation, as asked).
  • EXCLUDED from the 50%-SL and trailing strategies: their stoploss_results
    and trailing_results rows are DELETED, so both strategies' joins see
    nothing (invisible to win rate, counts, streaks, tiers — not a fake
    verdict, just absent). score_state='final' on every row so no rescore/
    maturation pass can re-price them back into existence.
  • Where an entry price is already stored, peak = 2x entry (keeps tile math
    consistent); where entry is NULL we set the multiple fields only — no
    invented dollar prices.
  • note records the manual origin for audit.
"""
from db import get_connection, transaction

IDS = [2791, 2728, 2731, 2716, 2750, 2759, 2711]
NOTE = "manual override 2026-09-24 (user): win 2.0x; excluded from SL/trail"

conn = get_connection()
rows = conn.execute(
    "SELECT id, token_symbol, status, is_win, entry_price_usd FROM calls "
    "WHERE id IN (%s) ORDER BY id" % ",".join("?" * len(IDS)), IDS).fetchall()
print("before:")
for r in rows:
    print("  ", tuple(r))

with transaction() as c:
    for r in rows:
        entry = r["entry_price_usd"]
        peak = entry * 2.0 if entry else None
        c.execute(
            """UPDATE calls SET
                 status='win', is_win=1,
                 peak_price_usd=COALESCE(?, peak_price_usd),
                 peak_profit_pct=100.0,
                 peak_multiple=2.0, max_multiple=2.0,
                 target_2x_reached=1,
                 which_threshold_first='2x',
                 score_state='final', pending_reason=NULL,
                 priced_at=datetime('now'), note=?
               WHERE id=?""",
            (peak, NOTE, r["id"]))
        c.execute("DELETE FROM stoploss_results WHERE call_id=?", (r["id"],))
        c.execute("DELETE FROM trailing_results WHERE call_id=?", (r["id"],))

print("after:")
for r in conn.execute(
    "SELECT id, token_symbol, status, is_win, entry_price_usd, peak_price_usd, "
    "peak_profit_pct, max_multiple, score_state FROM calls "
    "WHERE id IN (%s) ORDER BY id" % ",".join("?" * len(IDS)), IDS).fetchall():
    print("  ", tuple(r))
print("sl rows left:", conn.execute(
    "SELECT COUNT(*) FROM stoploss_results WHERE call_id IN (%s)"
    % ",".join("?" * len(IDS)), IDS).fetchone()[0])
print("trail rows left:", conn.execute(
    "SELECT COUNT(*) FROM trailing_results WHERE call_id IN (%s)"
    % ",".join("?" * len(IDS)), IDS).fetchone()[0])
