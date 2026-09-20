"""One-time repair: fill calls.token_symbol/token_name on rows priced before
the identity plumbing existed (deep-dive showed bare addresses).

Two sources, in order:
  1. token_meta (free — the pool resolver already fetched symbol/name for
     most tokens; it was simply never copied onto the calls row)
  2. DexScreener resolve_by_pool(pool_address) for rows with no meta row —
     also caches into token_meta so this runs once.

Idempotent; safe to re-run; never overwrites an existing non-empty symbol.
"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db import get_connection, transaction

def main():
    c = get_connection()
    # 1) token_meta fill (free)
    with c:
        n1 = c.execute("""
            UPDATE calls SET
              token_symbol = COALESCE(NULLIF(calls.token_symbol,''), tm.symbol),
              token_name   = COALESCE(NULLIF(calls.token_name,''),   tm.name)
            FROM token_meta tm
            WHERE tm.chain = calls.chain AND tm.address = calls.token_address
              AND (calls.token_symbol IS NULL OR calls.token_symbol='')
        """).rowcount
    print(f"filled from token_meta: {n1} rows")

    # 2) remaining decided rows with a pool_address -> DexScreener pair lookup
    todo = c.execute("""
        SELECT DISTINCT chain, pool_address, token_address FROM calls
        WHERE (token_symbol IS NULL OR token_symbol='')
          AND status IN ('win','loss') AND pool_address IS NOT NULL
    """).fetchall()
    print(f"rows needing DexScreener: {len(todo)} distinct pools")
    if todo:
        from pipeline import _get_ds_client, _DS_CHAIN
        from pricing.backtest import upsert_token_meta
        ds = _get_ds_client()
        fixed = 0
        for row in todo:
            try:
                info = ds.resolve_by_pool(row["pool_address"], chain=_DS_CHAIN[row["chain"]])
            except Exception as e:
                print(f"  lookup failed {row['pool_address'][:12]}: {e}")
                continue
            if info and (info.get("symbol") or info.get("name")):
                with transaction() as conn:
                    conn.execute("""
                        UPDATE calls SET
                          token_symbol = COALESCE(NULLIF(token_symbol,''), ?),
                          token_name   = COALESCE(NULLIF(token_name,''), ?)
                        WHERE chain=? AND pool_address=?
                          AND (token_symbol IS NULL OR token_symbol='')
                    """, (info.get("symbol"), info.get("name"),
                          row["chain"], row["pool_address"]))
                upsert_token_meta({**info, "token_address": row["token_address"]},
                                  chain=row["chain"])
                fixed += 1
        print(f"filled from DexScreener: {fixed} pools")

    left = c.execute("""
        SELECT COUNT(*) n FROM calls
        WHERE (token_symbol IS NULL OR token_symbol='')
          AND status IN ('win','loss')""").fetchone()
    print("decided rows still without a symbol:", left["n"],
          "(no pool anywhere -> unpriceable-class rows; or pending rows fix on next rescore)")

if __name__ == "__main__":
    main()
