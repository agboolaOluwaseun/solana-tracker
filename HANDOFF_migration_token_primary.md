# HANDOFF — token-primary price_cache migration (paused mid-flight, 2026-09-28)

## User rulings locked (verbatim intent)
- KEEP = exactly the curves displayed verdicts were computed from (calls.pool_address for
  GT-priced; 'birdeye' bucket for rescued). DELETE buckets A (flipped corpses: museic/META
  42 rows, DRUGS/BIO 165, ~93 groups), B (never-priced orphans ~145 groups), C (birdeye-legacy
  residue on non-rescued ~152 groups). Tie at same (chain,token,agg,ts): GT pool wins
  (verdicts are stored final, so display identical either way).
- "After deleting, there shouldnt be any difference on the frontend" = acceptance criterion.
- Chain column: keep in PK (insurance vs cross-chain mint collision; 1 exists today).

## CRITICAL STATE HAZARD
- kolfi.db is STILL old pool-primary schema. Working tree code is ALREADY rewired for
  token-primary → **tests will FAIL until migration --apply runs**. Running uvicorn (old code
  in memory) is unaffected; DO NOT restart it until migration applied.
- Migration dry-run validated: old 611,576 | keep 398,336 (birdeye 43,271) | GT-wins drops 654
  | DELETE 213,240 | unique 397,682.

## DONE (uncommitted, all lint-checked)
- pricing/cache.py — REWRITTEN token-primary API: store_candles(token, agg, candles,
  chain=, pool_address=, source=, conn=), load_candles(token, agg, start, end, chain=None);
  NETWORK_TO_CHAIN + chain_for(); norm_agg(); ON CONFLICT upsert.
- pricing/strategy7d_cache.py — make_cached_fetchers(conn, client_v2, chain=None);
  load_cached/store_candles read/write by (token, aggregate); chain derived from
  client_v2.network via chain_for. Import added.
- pricing/geckoterminal_v2.py + geckoterminal.py — fetch_ohlcv cache calls → new API
  (load_candles(token, aggregate, …, chain=chain_for(self.network)); store passes
  chain + pool provenance).
- pricing/backtest.py — backtest_call_birdeye gained chain="sol"; birdeye stores via
  store_candles(token, "minute", …, chain, source="birdeye"); cache reads token-only.
- pipeline.py — make_cached_fetchers(..., chain=chain); _trailing_series reads by
  token_address (pool var removed; _cached_mins by token); SL backfill loader =
  load_candles(token, "minute"); birdeye fallback call-sites pass chain=chain
  (verify line ~1605 got chain — the 3rd call-site used a different kwarg layout).
- schema_unified.sql — new price_cache DDL (PK chain,token,aggregate,candle_ts; pool
  nullable provenance).
- scripts/normalize_expired.py — _last_close by token.
- scripts/migrate_price_cache_token_primary.py — full migration w/ backup+VACUUM,
  --apply flag, dry-run matches above numbers.

## TODO (in order)
1. pricing/birdeye_rescue.py — STORE the rescued series at fetch time (write hourly+minute
   rows via pricing.cache.store_candles(token, "hour"/"minute", …, chain=corrected,
   source="birdeye", pool_address=None) inside try_rescue main path + _confirm_on_minutes
   path). Needs conn → use db.transaction() or get_connection(); import chain_for or map
   TO_INTERNAL directly (chain already internal here via TO_INTERNAL.get(bc)).
2. Verify pipeline line ~1602-1610 backtest_call_birdeye site got chain=chain.
3. Tests fixtures: tests/test_live_scoring.py CREATE TABLE price_cache (lines ~196-203) →
   new schema; its FakeGT network='solana' → chain 'sol' auto-derived; check any INSERT
   fixtures. tests/test_wick_filter.py — verify no pool-addressed cache SQL (its repair_*
   tests are pure-function, fine). Run .venv python -m pytest tests/ -q, fix fallout.
4. scripts/rescore_channel.py + scripts/correct_shift.py + scripts/reconcile_stoploss.py +
   scripts/audit_winrate.py — old-API importers (one-off scripts; patch signature-only
   where cheap, else leave with a NOTE header that they predate the migration).
5. Run migration --apply (backs up kolfi.db first). Confirm new rowcount 397,682.
6. Zero-diff proof: scripts/snapshot_visible_stats.py pre-saved /tmp/kolfi_stats_pre.json?
   — NOT yet re-run: run it BEFORE apply, run after, diff must be empty. (Script exists;
   earlier attempt errored on analysis.windowed.channel_stats — script now queries SQL
   directly, re-run: .venv python scripts/snapshot_visible_stats.py /tmp/kolfi_stats_pre.json)
7. Re-run: trail backfill? NO — verdicts stored final; migration must NOT re-score. Only
   sanity: SELECT a rescued call's trail row exists (73 from sweep already persisted).
8. Full tests 151/151 target; import api.server check; git add ALL + commit (message:
   token-primary price_cache, user ruling 2026-09-28, delete-A+B+C per display-provenance).
9. Tell user: restart uvicorn NOW activates everything.

## Anchors
- rehearsal scripts: /tmp/migration_rehearsal.py /tmp/migration_final_report.py /tmp/buckets*.py
- museic mint 0xed97b68ae1be330963eb161a11beb1cf5fe71ba3, good pool 0xc59d4307…, corpse 0xb5600a49…
- commits so far: 4298f66 (anchor fix) → 8e8533a (wick filter) → 2e1a111 (embed trail in rescue)
  → 977d4ac, 1f1a56c, bcbf053 earlier. All wick/anchor/rescue work committed; THIS migration
  is the only uncommitted batch.
