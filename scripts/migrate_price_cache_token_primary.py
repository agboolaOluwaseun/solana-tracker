"""Migrate price_cache: pool-primary -> token-primary keying (user ruling 2026-09-28).

  KEEP  = exactly the curves displayed verdicts were computed from
          (calls.pool_address for GT-priced calls; the 'birdeye' bucket for
          rescued calls). Same-ts tie between two consumed sources: GT pool
          wins (final verdicts are stored, not recomputed, so display is
          identical either way).
  DELETE = everything else: flipped-pool corpses (museic META, DRUGS BIO),
          never-priced orphans, reprice-tool residue on non-rescued tokens.

New schema: PRIMARY KEY (chain, token_address, aggregate, candle_ts);
pool_address + source demoted to provenance stamps (pool NULL for birdeye).

  python scripts/migrate_price_cache_token_primary.py          # dry-run report
  python scripts/migrate_price_cache_token_primary.py --apply  # backup + run
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "kolfi.db"


def run(c: sqlite3.Connection, apply: bool) -> None:
    c.row_factory = sqlite3.Row
    n_old = c.execute("SELECT COUNT(*) FROM price_cache").fetchone()[0]
    print(f"old rows: {n_old:,}")

    # ---- consumed map: (token, pool) -> chain; newest call wins ----
    consumed: dict[tuple[str, str], str] = {}
    for r in c.execute("""
        SELECT token_address t, pool_address p, chain,
               COALESCE(note,'')||COALESCE(engine,'') x
        FROM calls WHERE token_address IS NOT NULL AND token_address <> ''
        ORDER BY id ASC"""):
        p = (r["p"] or "").strip()
        if "birdeye" in r["x"].lower() or p == "birdeye":
            # the displayed verdict rode the Birdeye series; whatever pool is
            # stamped on the row (often the FLIPPED one — DRUGS/G7k3) is NOT
            # what produced the verdict, so it is NOT consumed (user ruling:
            # META/BIO corpses get deleted).
            consumed[(r["t"], "birdeye")] = r["chain"]
        elif p and p != "unknown":
            consumed[(r["t"], p)] = r["chain"]

    # ---- classify every row ----
    rows = c.execute("""SELECT token_address, pool_address, aggregate, candle_ts,
        open, high, low, close, volume, source FROM price_cache""").fetchall()
    kept: dict[tuple, tuple] = {}      # (chain,tok,agg,ts) -> (rank, row)
    stats = {"delete": 0, "keep": 0, "tie_drop": 0, "birdeye_kept": 0}
    for g in rows:
        chain = consumed.get((g["token_address"], g["pool_address"]))
        if chain is None:
            stats["delete"] += 1
            continue
        stats["keep"] += 1
        if g["pool_address"] == "birdeye":
            stats["birdeye_kept"] += 1
        rank = 1 if g["pool_address"] == "birdeye" else 0   # GT pool wins ties
        key = (chain, g["token_address"], g["aggregate"], g["candle_ts"])
        prev = kept.get(key)
        if prev is None or rank < prev[0]:
            if prev is not None:
                stats["tie_drop"] += 1
            kept[key] = (rank, g)
        else:
            stats["tie_drop"] += 1
    print(f"KEEP {stats['keep']:,} (birdeye-sourced {stats['birdeye_kept']:,}) | "
          f"same-ts GT-wins drops {stats['tie_drop']:,} | DELETE {stats['delete']:,}")
    print(f"unique rows after dedup: {len(kept):,}")

    if not apply:
        print("dry-run — nothing written")
        return

    # ---- build + swap ----
    c.execute("""
        CREATE TABLE price_cache_new (
            chain          TEXT NOT NULL,
            token_address  TEXT NOT NULL,
            aggregate      TEXT NOT NULL,
            candle_ts      TEXT NOT NULL,
            pool_address   TEXT,
            source         TEXT NOT NULL DEFAULT 'geckoterminal',
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            cached_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (chain, token_address, aggregate, candle_ts)
        )""")
    c.executemany(
        """INSERT INTO price_cache_new (chain, token_address, aggregate, candle_ts,
               pool_address, source, open, high, low, close, volume)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        [(chain, tok, agg, ts,
          None if g["pool_address"] == "birdeye" else g["pool_address"],
          "birdeye" if g["pool_address"] == "birdeye"
          else (g["source"] or "geckoterminal"),
          g["open"], g["high"], g["low"], g["close"], g["volume"])
         for (chain, tok, agg, ts), (_rank, g) in kept.items()],
    )
    c.execute("DROP TABLE price_cache")
    c.execute("ALTER TABLE price_cache_new RENAME TO price_cache")
    c.execute("CREATE INDEX idx_pc_token ON price_cache(token_address, aggregate)")
    c.commit()
    n = c.execute("SELECT COUNT(*) FROM price_cache").fetchone()[0]
    print(f"APPLIED: new price_cache = {n:,} rows ({n_old - n:,} purged)")


def main() -> None:
    apply = "--apply" in sys.argv
    if apply:
        bak = DB.with_suffix(f".db.bak-{datetime.now():%Y%m%dT%H%M%S}")
        shutil.copy2(DB, bak)
        print(f"backup: {bak.name}")
    c = sqlite3.connect(DB)
    try:
        run(c, apply)
        if apply:
            c.execute("VACUUM")
            print("VACUUM done")
    finally:
        c.close()


if __name__ == "__main__":
    main()
