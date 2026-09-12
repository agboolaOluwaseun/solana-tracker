"""One-shot migration: legacy Solana DB + Robinhood sim DBs -> unified kolfi.db.

Reads all sources in READ-ONLY mode (sqlite URI mode=ro). Never writes to
solana_tracker.db. Builds the target from scratch via schema_unified.sql.

Channel merge rule (approved in plan v2):
  * Solana channels port 1:1, preserving id + telegram_channel_id.
  * Robinhood @x666calls merges into the SAME channel row (telegram id
    1903316574, the v2 sim's 666 channel) — chain lives on calls.
  * Robinhood @Oliverdegengamble (telegram id 4301119799) is a new row.

Stoploss fold rule (plan B2):
  * solana stoploss_results rows copy 1:1.
  * Robinhood 7d stoploss verdicts become real stoploss_results rows:
    status=stoploss_status, is_win=(stoploss_status=='win'),
    hit_stoploss=1 when the plain strategy won but stoploss lost
    (i.e. -50% fired before 2x). Unknown fields stay NULL — no invention.

Aggregate labelling for price_cache:
  * RH rows already carry 'minute'/'hour'.
  * Solana rows have no aggregate column: a (pool,token) pair that contains
    ANY candle whose minute != 0 is minute-granular (all its rows -> 'minute');
    otherwise -> 'hour'.

Usage:  .venv/bin/python scripts/migrate_to_unified_db.py [--target PATH] [--replace]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SOL_DB = PROJECT_ROOT / "solana_tracker.db"
RH_DIR = Path("/Users/agboolaoluwaseun/ZCodeProject/robinhood_tracker")
RH_DBS = [RH_DIR / "sim_666_rh.db", RH_DIR / "sim_oliver_rh.db"]
SCHEMA_FILE = PROJECT_ROOT / "schema_unified.sql"

# Robinhood channel_tag -> (telegram_channel_id, username, title)
RH_CHANNELS = {
    "@x666calls": (1903316574, "x666calls", "666 🔥 Calls"),
    "@Oliverdegengamble": (4301119799, "Oliverdegengamble", "OLIVER DEGEN GAMBLES"),
}

SOL_CALL_COLS = [
    "id", "channel_id", "message_id", "raw_text", "token_address", "token_symbol",
    "token_name", "call_timestamp", "week_index", "pool_address", "entry_price_usd",
    "peak_price_usd", "peak_timestamp", "peak_profit_pct", "peak_multiple", "is_win",
    "status", "pending_reason", "priced_at", "error", "pre_target_drawdown_frac",
    "created_at",
]


def ro(path: Path) -> sqlite3.Connection:
    assert path.exists(), f"missing source: {path}"
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=str(PROJECT_ROOT / "kolfi.db"))
    ap.add_argument("--replace", action="store_true", help="overwrite existing target")
    args = ap.parse_args()

    target = Path(args.target)
    if target.exists():
        if not args.replace:
            print(f"REFUSING: {target} exists (pass --replace to rebuild it)")
            return 1
        target.unlink()

    out = sqlite3.connect(target)
    out.row_factory = sqlite3.Row
    out.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))

    report: list[tuple[str, int, int]] = []  # (label, source_count, ported_count)

    # ---------------------------------------------------------------- channels
    sol = ro(SOL_DB)
    sol_channels = sol.execute("SELECT * FROM channels ORDER BY id").fetchall()
    for ch in sol_channels:
        out.execute(
            "INSERT INTO channels (id,telegram_channel_id,username,title,window_start,"
            "window_end,created_at) VALUES (?,?,?,?,?,?,?)",
            tuple(ch[k] for k in ch.keys()),
        )
    n_ch_sol = out.execute("SELECT COUNT(*) n FROM channels").fetchone()["n"]
    report.append(("channels (solana 1:1)", len(sol_channels), n_ch_sol))

    tg_to_cid = {ch["telegram_channel_id"]: ch["id"] for ch in sol_channels}
    rh_cid: dict[str, int] = {}
    merged = new = 0
    for db_path in RH_DBS:
        src = ro(db_path)
        tags = [r["channel_tag"] for r in src.execute("SELECT DISTINCT channel_tag FROM calls")]
        for tag in tags:
            tg_id, username, title = RH_CHANNELS[tag]
            if tg_id in tg_to_cid:
                rh_cid[tag] = tg_to_cid[tg_id]
                merged += 1
            else:
                lo = src.execute("SELECT MIN(call_timestamp) t FROM calls WHERE channel_tag=?", (tag,)).fetchone()["t"]
                hi = src.execute("SELECT MAX(call_timestamp) t FROM calls WHERE channel_tag=?", (tag,)).fetchone()["t"]
                cur = out.execute(
                    "INSERT INTO channels (telegram_channel_id,username,title,window_start,window_end)"
                    " VALUES (?,?,?,?,?)",
                    (tg_id, username, title, lo or "unknown", hi or "unknown"),
                )
                rh_cid[tag] = cur.lastrowid
                tg_to_cid[tg_id] = cur.lastrowid
                new += 1
        src.close()
    print(f"channels: {len(sol_channels)} solana ported | {merged} RH tags merged into "
          f"existing | {new} new RH-only")

    # ------------------------------------------------------------------- calls
    sol_call_rows = sol.execute(f"SELECT {', '.join(SOL_CALL_COLS)} FROM calls ORDER BY id").fetchall()
    for r in sol_call_rows:
        out.execute(
            "INSERT INTO calls (id,channel_id,chain,message_id,raw_text,token_address,"
            "token_symbol,token_name,call_timestamp,week_index,pool_address,entry_price_usd,"
            "peak_price_usd,peak_timestamp,peak_profit_pct,peak_multiple,is_win,status,"
            "pending_reason,priced_at,error,pre_target_drawdown_frac,engine,scored_window,"
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'legacy','12h',?)",
            (r["id"], r["channel_id"], "sol", r["message_id"], r["raw_text"],
             r["token_address"], r["token_symbol"], r["token_name"], r["call_timestamp"],
             r["week_index"], r["pool_address"], r["entry_price_usd"], r["peak_price_usd"],
             r["peak_timestamp"], r["peak_profit_pct"], r["peak_multiple"], r["is_win"],
             r["status"], r["pending_reason"], r["priced_at"], r["error"],
             r["pre_target_drawdown_frac"], r["created_at"]),
        )
    report.append(("calls (solana legacy)", len(sol_call_rows),
                   out.execute("SELECT COUNT(*) n FROM calls WHERE chain='sol'").fetchone()["n"]))

    next_id = (sol_call_rows[-1]["id"] + 1) if sol_call_rows else 1
    rh_total = 0
    for db_path in RH_DBS:
        src = ro(db_path)
        for r in src.execute("SELECT * FROM calls ORDER BY id").fetchall():
            cid = rh_cid[r["channel_tag"]]
            decided = r["plain_status"] in ("win", "loss")
            max_mult = None
            if decided and r["peak_price_usd"] is not None and r["entry_price_usd"]:
                max_mult = r["peak_price_usd"] / r["entry_price_usd"]
            which = r["which_first"] if decided else None
            if which == "screening LOS":
                which = None  # unified enum is '2x'|'minus50'; screening note kept in note
            t2x = m50 = 0
            if decided:
                t2x = 1 if r["plain_status"] == "win" else 0
                m50 = 1 if r["plain_status"] == "loss" else 0
                # plain win but stoploss loss -> both thresholds were reached
                if (r["stoploss_status"] or "") == "loss":
                    t2x = m50 = 1
            out.execute(
                "INSERT INTO calls (id,channel_id,chain,message_id,raw_text,token_address,"
                "token_symbol,call_timestamp,pool_address,entry_price_usd,peak_price_usd,"
                "peak_timestamp,peak_profit_pct,peak_multiple,is_win,status,engine,"
                "scored_window,max_multiple,which_threshold_first,target_2x_reached,"
                "minus_50_reached,api_requests_used,note,priced_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
                (next_id, cid, "robinhood", r["message_id"], r["raw_text"],
                 r["token_address"], r["token_symbol"], r["call_timestamp"],
                 r["pool_address"], r["entry_price_usd"], r["peak_price_usd"],
                 r["peak_timestamp"], r["peak_profit_pct"], max_mult,
                 1 if r["plain_status"] == "win" else 0,
                 r["plain_status"] or "pending",
                 "7d", "7d", max_mult, which, t2x, m50,
                 r["api_requests_used"], r["note"]),
            )
            next_id += 1
            rh_total += 1
        src.close()
    report.append(("calls (robinhood 7d)", rh_total,
                   out.execute("SELECT COUNT(*) n FROM calls WHERE chain='robinhood'").fetchone()["n"]))

    # -------------------------------------------------------- stoploss_results
    n = 0
    for r in sol.execute("SELECT * FROM stoploss_results").fetchall():
        out.execute(
            "INSERT INTO stoploss_results (id,call_id,entry_price_usd,peak_price_usd,"
            "peak_timestamp,peak_profit_pct,hit_stoploss,stoploss_timestamp,is_win,status,"
            "error,computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(r[k] for k in r.keys()),
        )
        n += 1
    report.append(("stoploss_results (solana)", n, n))

    folded = 0
    for db_path in RH_DBS:
        src = ro(db_path)
        for r in src.execute(
            "SELECT * FROM calls WHERE stoploss_status IS NOT NULL AND stoploss_status != ''"
        ).fetchall():
            cid = rh_cid[r["channel_tag"]]
            uid = out.execute(
                "SELECT id FROM calls WHERE chain='robinhood' AND channel_id=?"
                " AND message_id=? AND token_address=?",
                (cid, r["message_id"], r["token_address"]),
            ).fetchone()["id"]
            is_win = 1 if r["stoploss_status"] == "win" else 0
            hit = 1 if (r["stoploss_status"] == "loss" and r["plain_status"] == "win") else 0
            out.execute(
                "INSERT INTO stoploss_results (call_id,entry_price_usd,peak_price_usd,"
                "hit_stoploss,is_win,status,computed_at) VALUES (?,?,?,?,?,?,datetime('now'))"
                " ON CONFLICT(call_id) DO UPDATE SET status=excluded.status,"
                " is_win=excluded.is_win, hit_stoploss=excluded.hit_stoploss",
                (uid, r["entry_price_usd"], r["peak_price_usd"], hit, is_win,
                 r["stoploss_status"]),
            )
            folded += 1
        src.close()
    sl_target = out.execute(
        "SELECT COUNT(*) n FROM stoploss_results WHERE call_id > (SELECT MAX(id) FROM calls WHERE chain='sol')"
    ).fetchone()["n"]
    report.append(("stoploss_results (RH folded)", folded, sl_target))

    # ------------------------------------------------------------- token_meta
    n = 0
    for r in sol.execute("SELECT * FROM token_meta").fetchall():
        out.execute(
            "INSERT OR IGNORE INTO token_meta (chain,address,symbol,name,dex_pool_id,"
            "liquidity_usd,first_seen) VALUES ('sol',?,?,?,?,?,?)",
            (r["address"], r["symbol"], r["name"], r["dex_pool_id"], r["liquidity_usd"],
             r["first_seen"]),
        )
        n += 1
    report.append(("token_meta (solana)", n,
                   out.execute("SELECT COUNT(DISTINCT address) n FROM token_meta WHERE chain='sol'").fetchone()["n"]))
    n = 0
    for db_path in RH_DBS:
        src = ro(db_path)
        for r in src.execute("SELECT * FROM token_meta").fetchall():
            out.execute(
                "INSERT OR IGNORE INTO token_meta (chain,address,symbol,dex_pool_id,"
                "liquidity_usd,first_seen) VALUES ('robinhood',?,?,?,?,?)",
                (r["address"], r["symbol"], r["dex_pool_id"], r["liquidity_usd"],
                 r["first_seen"]),
            )
            n += 1
        src.close()
    report.append(("token_meta (robinhood)", n,
                   out.execute("SELECT COUNT(DISTINCT address) n FROM token_meta WHERE chain='robinhood'").fetchone()["n"]))

    # ----------------------------------------------------------- price_cache
    minute_pairs: set[tuple[str, str]] = set()
    all_pairs: set[tuple[str, str]] = set()
    n_sol_candles = 0
    for pa, ta, ts in sol.execute("SELECT pool_address, token_address, candle_ts FROM price_cache"):
        n_sol_candles += 1
        key = (pa, ta)
        all_pairs.add(key)
        if len(ts) >= 16 and ts[14:16] != "00":
            minute_pairs.add(key)

    def sol_aggregate(key: tuple[str, str]) -> str:
        return "minute" if key in minute_pairs else "hour"

    for r in sol.execute(
        "SELECT pool_address, token_address, candle_ts, open, high, low, close, volume,"
        " source, cached_at FROM price_cache"
    ):
        out.execute(
            "INSERT OR IGNORE INTO price_cache (pool_address,token_address,aggregate,"
            "candle_ts,open,high,low,close,volume,source,cached_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (r["pool_address"], r["token_address"],
             sol_aggregate((r["pool_address"], r["token_address"])),
             r["candle_ts"], r["open"], r["high"], r["low"], r["close"], r["volume"],
             r["source"], r["cached_at"]),
        )
    sol_pc_ported = out.execute("SELECT COUNT(*) n FROM price_cache").fetchone()["n"]
    report.append(("price_cache (solana)", n_sol_candles, sol_pc_ported))

    n_rh_candles = 0
    for db_path in RH_DBS:
        src = ro(db_path)
        for r in src.execute("SELECT * FROM price_cache").fetchall():
            out.execute(
                "INSERT OR IGNORE INTO price_cache (pool_address,token_address,aggregate,"
                "candle_ts,open,high,low,close,volume,source) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (r["pool_address"], r["token_address"], r["aggregate"], r["candle_ts"],
                 r["open"], r["high"], r["low"], r["close"], r["volume"], "geckoterminal"),
            )
            n_rh_candles += 1
        src.close()
    total_pc = out.execute("SELECT COUNT(*) n FROM price_cache").fetchone()["n"]
    report.append(("price_cache (robinhood)", n_rh_candles, total_pc - sol_pc_ported))

    out.commit()

    # ------------------------------------------------------------- reconciliation
    print("\n=== RECONCILIATION REPORT -> %s ===" % target)
    print(f"{'table':34} {'source':>8} {'target':>8}")
    for label, s, t in report:
        print(f"{label:34} {s:>8} {t:>8}")

    print("\n--- calls by chain & status (target) ---")
    for r in out.execute("SELECT chain, status, COUNT(*) n FROM calls GROUP BY chain, status ORDER BY chain, status"):
        print(f"  {r['chain']:10} {r['status']:18} {r['n']:>5}")
    print("--- price_cache by aggregate (target) ---")
    for r in out.execute("SELECT aggregate, COUNT(*) n FROM price_cache GROUP BY aggregate"):
        print(f"  {r['aggregate']:8} {r['n']:>8}")

    print("\n--- parity checks ---")
    ok = True
    src_calls = sol.execute("SELECT COUNT(*) n, SUM(status='win') w FROM calls").fetchone()
    tgt_calls = out.execute("SELECT COUNT(*) n, SUM(status='win') w FROM calls WHERE chain='sol'").fetchone()
    p = src_calls["n"] == tgt_calls["n"] and src_calls["w"] == tgt_calls["w"]
    ok &= p
    print(f"  solana calls     src={src_calls['n']} wins={src_calls['w']} | tgt={tgt_calls['n']} wins={tgt_calls['w']}  {'OK' if p else 'MISMATCH'}")
    src_sl = sol.execute("SELECT COUNT(*) n, SUM(status='win') w FROM stoploss_results").fetchone()
    tgt_sl = out.execute("SELECT COUNT(*) n, SUM(status='win') w FROM stoploss_results"
                         " WHERE call_id <= (SELECT MAX(id) FROM calls WHERE chain='sol')").fetchone()
    p = src_sl["n"] == tgt_sl["n"] and src_sl["w"] == tgt_sl["w"]
    ok &= p
    print(f"  solana stoploss  src={src_sl['n']} wins={src_sl['w']} | tgt={tgt_sl['n']} wins={tgt_sl['w']}  {'OK' if p else 'MISMATCH'}")
    src_pc = sol.execute("SELECT COUNT(*) n FROM price_cache").fetchone()["n"]
    tgt_sol_pc = out.execute("SELECT COUNT(*) n FROM price_cache").fetchone()["n"] - n_rh_candles
    p = src_pc == tgt_sol_pc
    ok &= p
    print(f"  solana candles   src={src_pc} | tgt={tgt_sol_pc}  {'OK' if p else 'MISMATCH'}")
    for db_path in RH_DBS:
        s = ro(db_path)
        sn = s.execute("SELECT COUNT(*) n FROM calls").fetchone()["n"]
        sw = s.execute("SELECT COUNT(*) n FROM calls WHERE plain_status='win'").fetchone()["n"]
        tag = s.execute("SELECT DISTINCT channel_tag FROM calls").fetchone()["channel_tag"]
        t = out.execute("SELECT COUNT(*) n, SUM(status='win') w FROM calls"
                        " WHERE chain='robinhood' AND channel_id=?", (rh_cid[tag],)).fetchone()
        p = sn == t["n"] and sw == t["w"]
        ok &= p
        print(f"  {tag:18} src={sn} wins={sw} | tgt={t['n']} wins={t['w']}  {'OK' if p else 'MISMATCH'}")
        s.close()

    print("\nMIGRATION", "COMPLETE" if ok else "COMPLETED WITH PARITY FAILURES — DO NOT CUTOVER", "->", target)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
