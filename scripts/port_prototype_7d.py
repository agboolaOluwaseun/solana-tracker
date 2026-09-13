"""C3 (revised): replace legacy-engine results with prototype exact-spec 7d
data in kolfi.db for every channel already evaluated in test_eval.db.

Prototype evaluated so far: 666 only (88 calls, all matched 1:1 into
kolfi channel 26 == @x666calls via token_address + minute of call_timestamp).

Per matched call:
  calls            <- engine='7d', scored_window='7d', pool_address from the
                      sim call row, all inline 7d fields from eval_results,
                      plain decision: entry_price_usd/peak_price_usd/
                      peak_profit_pct/is_win/status mirror the 7d fields
                      (same semantics legacy readers expect)
  stoploss_results <- status/entry/peak/hit_stoploss from stoploss_status
                      (same shape the pipeline persists)
  token_meta       <- ensure pool exists for chain 'sol'

kolfi.db stays rebuildable (migrate_to_unified_db.py), so --apply is safe.
Dry-run by default.
"""
import argparse
import os
import sqlite3
import sys

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TE_DB = os.path.join(PROJ, "test_eval.db")
TARGET_DB = os.path.join(PROJ, "kolfi.db")

# channel_tag in test_eval.db -> channel id in kolfi.db (by telegram username)
TAG_TO_KOLFI = {
    "666": "x666calls",
    "eleetmo": "eleetmoramblings",
    "trenches": "trenchescalls",
}

SL_STOP_PCT = 50.0  # the engine's stop threshold


def _norm(ts: str) -> str:
    return ts.replace(" ", "T")[:16]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--target", default=TARGET_DB)
    args = ap.parse_args()

    te = sqlite3.connect(f"file:{TE_DB}?mode=ro", uri=True)
    te.row_factory = sqlite3.Row
    out = sqlite3.connect(args.target)
    out.row_factory = sqlite3.Row

    n_applied = 0
    for tag, username in TAG_TO_KOLFI.items():
        chan = out.execute(
            "SELECT id FROM channels WHERE lower(username)=lower(?)", (username,)
        ).fetchone()
        if not chan:
            print(f"[skip] {tag}: no kolfi channel {username}")
            continue
        cid = chan["id"]
        protos = te.execute(
            "SELECT e.*, c.token_address a, c.token_symbol s, c.pool_address "
            "FROM eval_results e JOIN calls c ON c.channel_tag=e.channel_tag AND c.id=e.id "
            "WHERE e.channel_tag=?",
            (tag,),
        ).fetchall()
        if not protos:
            print(f"[skip] {tag}: no prototype eval rows")
            continue

        applied = matched = 0
        for p in protos:
            pts = _norm(p["call_timestamp"])
            row = out.execute(
                "SELECT id FROM calls WHERE channel_id=? AND chain='sol' "
                "AND lower(token_address)=lower(?) "
                "AND replace(substr(call_timestamp,1,16),' ','T')=?",
                (cid, p["a"], pts),
            ).fetchone()
            if not row:
                print(f"  ! no kolfi match for {tag} call {p['id']} {p['a'][:10]} {pts}")
                continue
            matched += 1
            call_id = row["id"]

            is_win = 1 if p["plain_status"] == "win" else 0
            # legacy-parallel status: preserve unpriceable separately from a
            # decided loss (the 7d parity harness counts them as 'unp').
            status = {"win": "win", "loss": "loss",
                      "unpriceable_loss": "unpriceable_loss"}[p["plain_status"]]
            sl_win = 1 if p["stoploss_status"] == "win" else 0
            sl_lost = p["stoploss_status"] == "loss"
            # legacy-parallel plain fields: entry = actual entry, peak = max
            # post-call price; status mirrors is_win exactly (legacy semantics
            # 'win'/'loss' are plain-strategy outcomes).
            out.execute(
                """
                UPDATE calls SET
                    engine='7d', scored_window='7d',
                    pool_address=COALESCE(?, pool_address),
                    status=?, is_win=?,
                    entry_price_usd=?, peak_price_usd=?, peak_timestamp=?,
                    peak_profit_pct=?, peak_multiple=?,
                    screening_entry_usd=?, screening_target_usd=?, target_usd=?,
                    max_price_usd=?, min_price_usd=?, max_multiple=?,
                    max_drawdown_pct=?, target_2x_reached=?, minus_50_reached=?,
                    time_2x_reached=?, time_minus_50_reached=?,
                    which_threshold_first=?, api_requests_used=?,
                    granular_analysis_required=?, option2_entry=?,
                    evaluation_end_timestamp=?, note=?, priced_at=?,
                    error=NULL
                WHERE id=?
                """,
                (
                    p["pool_address"],
                    status,
                    is_win,
                    p["actual_entry_price"],
                    p["maximum_price_after_call"],
                    None,
                    (p["maximum_multiple"] - 1.0) * 100.0
                    if p["maximum_multiple"] is not None
                    else None,
                    p["maximum_multiple"],
                    p["screening_entry_price"],
                    p["screening_target"],
                    p["actual_target"],
                    p["maximum_price_after_call"],
                    p["minimum_price_after_call"],
                    p["maximum_multiple"],
                    p["maximum_drawdown_pct"],
                    p["target_2x_reached"],
                    p["minus_50_reached"],
                    p["time_2x_reached"],
                    p["time_minus_50_reached"],
                    p["which_threshold_occurred_first"],
                    p["api_requests_used"],
                    1 if p["granular_analysis_required"] else 0,
                    0,
                    p["evaluation_end_timestamp"],
                    p["note"],
                    p["call_timestamp"],
                    call_id,
                ),
            )

            if p["stoploss_status"] in ("win", "loss"):
                out.execute(
                    """
                    INSERT INTO stoploss_results
                        (call_id, entry_price_usd, peak_price_usd, peak_timestamp,
                         peak_profit_pct, hit_stoploss, stoploss_timestamp,
                         is_win, status, computed_at)
                    VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))
                    ON CONFLICT(call_id) DO UPDATE SET
                        entry_price_usd=excluded.entry_price_usd,
                        peak_price_usd=excluded.peak_price_usd,
                        peak_timestamp=excluded.peak_timestamp,
                        peak_profit_pct=excluded.peak_profit_pct,
                        hit_stoploss=excluded.hit_stoploss,
                        stoploss_timestamp=excluded.stoploss_timestamp,
                        is_win=excluded.is_win,
                        status=excluded.status,
                        error=NULL,
                        computed_at=excluded.computed_at
                    """,
                    (
                        call_id,
                        p["actual_entry_price"],
                        p["maximum_price_after_call"],
                        None,
                        (p["maximum_multiple"] - 1.0) * 100.0
                        if p["maximum_multiple"] is not None
                        else None,
                        1 if sl_lost else 0,
                        p["time_minus_50_reached"] if sl_lost else None,
                        sl_win,
                        "win" if sl_win else "loss",
                    ),
                )
            else:
                # unpriceable under 7d too -> drop any stale legacy stoploss row
                out.execute(
                    "DELETE FROM stoploss_results WHERE call_id=?", (call_id,)
                )

            if p["pool_address"]:
                out.execute(
                    "INSERT OR IGNORE INTO token_meta (chain, address) VALUES ('sol', ?)",
                    (p["pool_address"],),
                )
            applied += 1
        print(f"[{tag}] matched={matched}/{len(protos)} applied={applied}")
        n_applied += applied

    if args.apply:
        out.commit()
        print(f"APPLIED {n_applied} calls -> {args.target}")
    else:
        out.rollback()
        print(f"DRY-RUN {n_applied} would-be-updated calls (pass --apply)")
    out.close()


if __name__ == "__main__":
    main()
