# Integration Plan v2: Unified DB (`kolfi.db`) + 7-Day Engine + Chain Toggle (SOL/RH/All) + Performance-Ranking Meter

> **For Hermes:** Implement task-by-task in order. Each workstream is independently shippable.
> Commit after every task. Never batch unrelated workstreams into one commit.
> This document is self-contained: an implementer with zero prior context must be able to execute it.

**Goal:** Port the legacy tracker into ONE new unified database (`kolfi.db`) that holds both chains
and every field the improved 7-day algorithm produces; make the 7-day screening engine the pricing
engine behind a flag; make ONE standard backfill price Solana AND Robinhood calls; expose
Solana / Robinhood / All everywhere in the KOLfi Next.js frontend; add the cumulative
"Performance Ranking" multiplier-tier meter (100x…2x, <2x) to the channel deep-dive, tracking the
selected timeframe.

**Architecture:**
- `schema_unified.sql` defines the new DB. `chain` is a **column on `calls`** (`'sol'|'robinhood'`),
  NOT on channels — one Telegram channel (e.g. 666) posts both kinds of tokens, so channels merge by
  `telegram_channel_id` and calls carry the chain. `?chain=all` becomes a plain SQL filter.
- The 7d result fields live **inline on `calls`** (no side table, no joins). Legacy-ported rows get
  `engine='legacy'` with those columns NULL (honest blanks).
- `stoploss_results` becomes a **compatibility VIEW over `calls`** so every existing reader
  (Streamlit stop-loss tabs, `analysis/stoploss.py`, `analysis/windowed.py`) keeps working unedited.
- `pricing/strategy7d.py` is the single chain-agnostic implementation of the frozen 7-day algorithm;
  fetchers are injected so caching / rate-limiting / network stay outside it.
- `PRICING_ENGINE=legacy|7d` env flag switches scoring; default `legacy` until shadow validation passes.
- Cutover = `.env` flip (`DB_PATH=kolfi.db`, `SCHEMA_FILE=schema_unified.sql`) after the migration
  report is reviewed. Rollback = delete those two lines. The legacy `solana_tracker.db` file is
  never modified and remains the rollback artifact.

**Tech stack:** Python 3.11 (stdlib + existing deps only — NO new packages), SQLite, Starlette API
(`api/server.py`), Next.js 14 + Tailwind + zustand (`frontend/`).

**User decisions baked in (2026-09-12):**
1. New DB name: `kolfi.db` (any name acceptable; this one chosen).
2. ONE merged channel row per Telegram channel id (666 = one row holding sol + robinhood calls).
3. Meter shows tiers for ALL rows (legacy included, via `COALESCE(max_multiple, peak_multiple)`),
   follows the selected timeframe (card day-pills 1/3/7/30 override; otherwise the global
   TimeFilter), and carries NO caption about window/engine.
4. Robinhood-only historical backfill (Workstream G), anchored to `preset_window("5m")` (Apr 1 when
   run in Sep 2026; tracks the month automatically).
5. Standard backfill must price BOTH chains in one run (Task D2).

---

## 0. Ground rules (read first — violations break the user's trust)

1. **`solana_tracker.db` is READ-ONLY forever in this plan.** Every write goes to `kolfi.db` (or a
   copy of it for tests). The migration reads legacy + sim DBs read-only.
2. **Do not delete or rewrite legacy scoring.** `pricing/backtest.py`, `pricing/geckoterminal.py`,
   `pricing/birdeye.py` stay as-is; the new engine runs alongside behind the flag. Legacy remains
   the fallback forever.
3. **Do not modify `../robinhood_tracker/`** (the prototype project). The main repo vendors ONLY the
   fetch/parse/resolve plumbing it needs (Workstream D1), with a sync-date header.
4. **Do not touch the Streamlit app (`ui/`).** All UI work is the Next.js frontend + `api/server.py`.
   The Streamlit app must keep working against `kolfi.db` unchanged (the view guarantees this).
5. **No new Python or npm dependencies.**
6. **Rate limits:** every GeckoTerminal request goes through the token-bucket limiter
   (`pricing/rate_limiter_v2.py`, ~5 RPM sustained).
7. Secrets live in `.env`; never print or commit them.
8. **Progress UI shows real counters only** (`priced X/Y`, `reqs used`) — never invented percentages.

### Glossary / existing anchors

| Thing | Where |
|---|---|
| Legacy scorer (12h × 1m) | `pricing/backtest.py::backtest_call`, `score_candles`, `score_candles_stoploss` |
| Prototype 7d algorithm (frozen spec source) | `scripts/eval_strategy.py::evaluate_call` (lines 159–294) |
| V2 GT client + limiter | `pricing/geckoterminal_v2.py::GeckoTerminalClientV2`, `pricing/rate_limiter_v2.py` |
| Pipeline entry points | `pipeline.py::run_backfill` (348), `reprice_calls` (567), `mature_pending_calls` (175) |
| Legacy schema + init | `schema.sql`, `db.py::init_db` |
| Read API | `api/server.py` (routes 646); analysis in `analysis/windowed.py`, `analysis/stoploss.py` |
| Frontend store / toggles | `frontend/src/store/uiStore.ts`, `components/StrategyToggle.tsx`, `TimeFilter.tsx`, `Navbar.tsx` |
| Deep-dive page | `frontend/src/app/channels/[handle]/page.tsx` |
| Robinhood prototype | `../robinhood_tracker/` — `parser.py`, `dexscreener.py`, `geckoterminal.py`, `rate_limiter.py`, `pipeline.py`, `db.py` (schema reference) |
| Robinhood sim DBs (migration sources) | `../robinhood_tracker/sim_666_rh.db`, `sim_oliver_rh.db` |
| 7d validation cache | `test_eval.db::eval_candles` (hourly+minute candles from the validated 666 run) |

### Frozen algorithm spec (the contract — do not "improve" it)

Per call, all times naive UTC:

1. **Req 1 (hourly):** hourly OHLCV `[call_hour_start, call_ts+7d)`.
2. `call_hour_candle` = hourly candle with `timestamp == call_hour_start`. If absent →
   `unpriceable_loss`, note `no call-hour candle`.
3. **Screening:** `screening_entry = call_hour_candle.low`; `screening_target = 2 × screening_entry`;
   `max_hourly_high = max(high of candles in [call_hour_start, call_ts+7d))`.
   If `max_hourly_high < screening_target` → **definite LOSS** (both strategies), stop, 1 request.
4. **Req 2 (minute):** minute OHLCV `[floor(call_ts to minute), call_hour_end)`.
5. **Entry:** candle at exact call minute; else candle containing `call_ts`; else **Option 2**:
   first minute candle with `call_ts < candle.ts <= call_ts + 5min` (flag `option2_entry=True`).
   If none or `open <= 0` → `unpriceable_loss`, note `no call-minute candle`.
6. `entry = entry_candle.open`; `target = 2×entry`; `stop = 0.5×entry`.
7. **Final path (chronological):** minute candles `>= entry_candle.ts` in the call hour, then hourly
   candles `[call_hour_end, call_ts+7d)`. The call-hour hourly candle is **discarded**.
8. Walk the path; record first `t2x` (`high >= target`) and first `t50` (`low <= stop`).
   - Both first appear in the **same minute candle** → `which_first = "same_candle"` → **WIN for
     both strategies** (user rule).
   - Both first appear in the **same hourly candle** → **Req 3**: minute candles for that hour;
     resolve order; same-minute rule re-applies; if minutes contradict the hour → `same_candle`.
   - Else `which_first = "2x" if t2x < t50 else "minus50"`.
9. Outcomes: `plain = win iff t2x is not None`. `stoploss = win iff which_first in ("2x","same_candle")`.
10. Derived: `max_price = max(high over path)`, `min_price = min(low over path)`,
    `max_multiple = max_price/entry`, `max_drawdown_pct = (1 - min_price/entry)×100`,
    `evaluation_end = call_ts + 7d`, `granular_analysis_required = (req 2 happened)`.
11. **Caching (aggregate-aware):** minute candles fully cached; hourly candles cached **except the
    call-hour candle** (never trusted, never persisted). Re-runs cost exactly 1 hourly request per
    call + 0/1 minute requests.

Validated reference numbers (must reproduce exactly in Task A3):
Solana 666 (`test_eval.db`): 88 calls → plain 42W/40L/6 unp (51.2%), stoploss 32W/50L/6 unp (39.0%),
146 requests (1.66/call), 29 one-req screening losses, 57 two-req, 1 three-req, 0 same-candle.

---

## Workstream A — Promote the prototype to `pricing/strategy7d.py`

### Task A1: Create the pure engine module

**Files:**
- Create: `pricing/strategy7d.py`

Port `scripts/eval_strategy.py` lines 62–294 into a **pure, connection-injected** module. No
globals, no `sys.argv`, no prints. Exact public surface:

```python
@dataclass
class Eval7dResult:
    status_plain: str          # 'win' | 'loss' | 'unpriceable_loss'
    status_stoploss: str       # same enum
    screening_entry_usd: float | None
    screening_target_usd: float | None
    entry_price_usd: float | None
    target_usd: float | None
    max_price_usd: float | None
    min_price_usd: float | None
    max_multiple: float | None
    max_drawdown_pct: float | None
    target_2x_reached: bool
    minus_50_reached: bool
    time_2x_reached: datetime | None
    time_minus_50_reached: datetime | None
    which_threshold_first: str   # '2x' | 'minus50' | 'same_candle' | 'none'
    api_requests_used: int
    granular_analysis_required: bool
    option2_entry: bool
    evaluation_end_timestamp: datetime
    pool_address: str | None
    note: str                  # 'screening LOSS' | 'no pool' | 'no call-hour candle' | ...

def evaluate_call_7d(
    pool_address: str,
    token_address: str,
    call_ts: datetime,                 # naive UTC
    fetch_hourly,                      # callable(pool, token, start, end) -> (list[PricePoint], int reqs)
    fetch_minute,                      # callable(pool, token, start, end) -> (list[PricePoint], int reqs)
    eval_days: int = 7,
    entry_grace_minutes: int = 5,
) -> Eval7dResult
```

- `fetch_hourly`/`fetch_minute` are injected so the caller owns caching + rate limiting + network.
  The module contains ONLY the frozen algorithm (steps 1–11).
- Also provide `resolve_order_in_hour(fetch_minute, pool, token, hour_candle, entry, target, stop)`
  exactly as prototype lines 133–156.
- Module docstring = the frozen spec above, verbatim.
- Second new file `pricing/strategy7d_cache.py`: `make_cached_fetchers(conn, client_v2)` returning
  `(fetch_hourly, fetch_minute)` implementing caching rule (step 11) against the unified
  aggregate-aware `price_cache` table using `client_v2._request` + `_parse_ohlcv` exactly like
  prototype lines 96–129. `conn` is passed in — tests inject sqlite3 `:memory:`.
  `client_v2.network` selects the chain network ('solana' | 'robinhood'); verify
  `GeckoTerminalClientV2.__init__` accepts a `network` param — if it hardcodes 'solana', add the
  parameter (one-line change, default 'solana').

**Verify:** `python -c "import pricing.strategy7d, pricing.strategy7d_cache"` succeeds; no network
call at import time.

**Commit:** `feat(pricing): pure chain-agnostic 7-day evaluation engine (strategy7d)`

### Task A2: Unit tests with synthetic candles

**Files:**
- Create: `tests/__init__.py`, `tests/test_strategy7d.py`

Build `PricePoint` lists by hand (no DB, no network; fetchers return the lists). One test per row:

| # | Scenario | Expected |
|---|---|---|
| 1 | Hourly highs never reach 2× call-hour LOW | `loss/loss`, `api_requests_used==1`, `granular False` |
| 2 | Screening passes, 2x at hour 3, no dip | `win/win`, `which_first=='2x'`, reqs==2 |
| 3 | Screening passes, -50% at hour 1, 2x at hour 5 | plain `win`, stoploss `loss`, `which_first=='minus50'` |
| 4 | 2x and -50% in the SAME minute candle (call hour) | `which_first=='same_candle'`, both `win`, reqs==2 |
| 5 | 2x and -50% first both inside one LATER hourly candle; minutes show 2x first | reqs==3, `which_first=='2x'` |
| 6 | Same as 5 but minutes show -50 first | reqs==3, stoploss `loss` |
| 7 | No minute candle at call minute; first candle 3 min after call | `option2_entry True`, priced |
| 8 | No minute candle within 5 min after call | `unpriceable_loss`, note `no call-minute candle` |
| 9 | No hourly candle at call hour | `unpriceable_loss`, note `no call-hour candle`, reqs==1 |
| 10 | Pre-call hourly spike above target but all post-call highs below screening target | `loss` (screening) — pre-call data can never create wins |
| 11 | Neither threshold ever hit | `loss/loss`, `which_first=='none'` |

Run: `.venv/bin/python -m pytest tests/test_strategy7d.py -v` → 11 passed.
(If pytest missing in `.venv`: `pip install pytest` into `.venv` only — dev dep, not app dep.)

**Commit:** `test(pricing): synthetic-candle coverage for 7d engine`

### Task A3: Parity harness vs the prototype

**Files:**
- Create: `scripts/parity_7d.py`

Open `test_eval.db`; for every `666` call run `evaluate_call_7d` with cache-backed fetchers pointed
at the SAME `eval_candles` table the prototype used (zero new API requests); diff against
`eval_666.csv` columns (plain, stoploss, reqs, entry, max_mult, which_first).
Expected: `MISMATCHES: 0`, totals `42W/40L/6unp`, `32W/50L`, `146 reqs`.
Run: `.venv/bin/python scripts/parity_7d.py`.

**Commit:** `test(pricing): parity harness 7d engine vs prototype CSV`

---

## Workstream B — Unified schema + one-shot migration to `kolfi.db`

### Task B1: `schema_unified.sql`

**Files:**
- Create: `schema_unified.sql`
- Modify: `config.py` — add `schema_file: str = field(default_factory=lambda: os.getenv("SCHEMA_FILE", "schema.sql"))`
- Modify: `db.py::init_db` — read `PROJECT_ROOT / settings.schema_file` instead of hardcoded
  `schema.sql`. Nothing else in `db.py` changes.

Full DDL (idempotent CREATEs; no ALTERs needed since the DB is new):

```sql
CREATE TABLE IF NOT EXISTS channels (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_channel_id INTEGER NOT NULL UNIQUE,
    username            TEXT,
    title               TEXT,
    window_start        TEXT NOT NULL,
    window_end          TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);   -- chain-agnostic: ONE row per Telegram channel (user decision #2)

CREATE TABLE IF NOT EXISTS token_meta (
    chain         TEXT NOT NULL,            -- 'sol' | 'robinhood'
    address       TEXT NOT NULL,
    symbol        TEXT,
    name          TEXT,
    dex_pool_id   TEXT,
    liquidity_usd REAL,
    first_seen    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (chain, address)
);

CREATE TABLE IF NOT EXISTS calls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id        INTEGER NOT NULL REFERENCES channels(id),
    chain             TEXT NOT NULL,                 -- 'sol' | 'robinhood'
    message_id        INTEGER NOT NULL,
    raw_text          TEXT,
    token_address     TEXT NOT NULL,
    token_symbol      TEXT,
    token_name        TEXT,
    call_timestamp    TEXT NOT NULL,
    week_index        INTEGER NOT NULL DEFAULT 1,
    -- legacy pricing fields (plain strategy semantics, as today)
    pool_address      TEXT,
    entry_price_usd   REAL,
    peak_price_usd    REAL,
    peak_timestamp    TEXT,
    peak_profit_pct   REAL,
    peak_multiple     REAL,
    is_win            INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'pending',   -- pending|win|loss|unpriceable_loss
    pending_reason    TEXT,
    priced_at         TEXT,
    -- stoploss strategy fields (folded; served to old readers via the view below)
    sl_entry_price_usd   REAL,
    sl_peak_price_usd    REAL,
    sl_peak_timestamp    TEXT,
    sl_peak_profit_pct   REAL,
    sl_hit_stoploss      INTEGER NOT NULL DEFAULT 0,
    sl_stoploss_timestamp TEXT,
    sl_is_win            INTEGER NOT NULL DEFAULT 0,
    sl_status            TEXT NOT NULL DEFAULT 'pending',
    sl_error             TEXT,
    -- 7d engine fields (NULL for engine='legacy' rows — honest blanks)
    engine                TEXT NOT NULL DEFAULT 'legacy',   -- legacy|7d
    screening_entry_usd   REAL,
    screening_target_usd  REAL,
    target_usd            REAL,
    max_price_usd         REAL,
    min_price_usd         REAL,
    max_multiple          REAL,
    max_drawdown_pct      REAL,
    target_2x_reached     INTEGER NOT NULL DEFAULT 0,
    minus_50_reached      INTEGER NOT NULL DEFAULT 0,
    time_2x_reached       TEXT,
    time_minus_50_reached TEXT,
    which_threshold_first TEXT,
    api_requests_used     INTEGER NOT NULL DEFAULT 0,
    granular_analysis_required INTEGER NOT NULL DEFAULT 0,
    option2_entry         INTEGER NOT NULL DEFAULT 0,
    evaluation_end_timestamp TEXT,
    note                  TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(channel_id, chain, message_id, token_address)
);
CREATE INDEX IF NOT EXISTS idx_calls_channel ON calls(channel_id);
CREATE INDEX IF NOT EXISTS idx_calls_chain ON calls(chain);
CREATE INDEX IF NOT EXISTS idx_calls_status ON calls(status);
CREATE INDEX IF NOT EXISTS idx_calls_timestamp ON calls(call_timestamp);

CREATE TABLE IF NOT EXISTS price_cache (
    pool_address   TEXT NOT NULL,
    token_address  TEXT NOT NULL,
    aggregate      TEXT NOT NULL,        -- 'minute' | 'hour'
    candle_ts      TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    source         TEXT NOT NULL DEFAULT 'geckoterminal',
    cached_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (pool_address, token_address, aggregate, candle_ts)
);
CREATE INDEX IF NOT EXISTS idx_price_cache_token ON price_cache(token_address);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES channels(id),
    chain TEXT NOT NULL DEFAULT 'sol',
    started_at TEXT NOT NULL, finished_at TEXT,
    mode TEXT NOT NULL, messages_scanned INTEGER NOT NULL DEFAULT 0,
    calls_found INTEGER NOT NULL DEFAULT 0, calls_priced INTEGER NOT NULL DEFAULT 0,
    calls_unpriceable INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running', error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Compatibility view: every legacy reader of stoploss_results keeps working
-- UNEDITED (Streamlit stop-loss tabs, analysis/stoploss.py, analysis/windowed.py).
CREATE VIEW IF NOT EXISTS stoploss_results AS
SELECT
    id AS id, id AS call_id,
    sl_entry_price_usd  AS entry_price_usd,
    sl_peak_price_usd   AS peak_price_usd,
    sl_peak_timestamp   AS peak_timestamp,
    sl_peak_profit_pct  AS peak_profit_pct,
    sl_hit_stoploss     AS hit_stoploss,
    sl_stoploss_timestamp AS stoploss_timestamp,
    sl_is_win           AS is_win,
    sl_status           AS status,
    sl_error            AS error,
    priced_at           AS computed_at
FROM calls;
```

Note: the view aliases `id AS call_id` so `JOIN stoploss_results sr ON sr.call_id = c.id` still
resolves. The legacy table also had its own `id` PK distinct from call_id; readers only use
`call_id` + the metric columns (verified by grep during B4).

**Verify:** `DB_PATH=/tmp/k1.db SCHEMA_FILE=schema_unified.sql .venv/bin/python -c "from db import init_db; init_db()"`
then `sqlite3 /tmp/k1.db ".tables"` lists channels, calls, token_meta, price_cache, ingestion_runs,
stoploss_results(view).

**Commit:** `feat(db): unified schema (chain column, inline 7d fields, stoploss view)`

### Task B2: One-shot migration `scripts/migrate_to_unified_db.py`

**Files:**
- Create: `scripts/migrate_to_unified_db.py`

Reads sources **read-only** (`sqlite3.connect(f"file:{p}?mode=ro", uri=True)`):
`solana_tracker.db`, `../robinhood_tracker/sim_666_rh.db`, `../robinhood_tracker/sim_oliver_rh.db`.
Writes ONLY to the target (default `kolfi.db`; **refuse if the file exists unless `--force`**).

Steps:
1. `init_db()` on target with `SCHEMA_FILE=schema_unified.sql`.
2. **channels:** port legacy rows as-is. Then for each sim DB channel row: if its
   `telegram_channel_id` already exists (e.g. 666) → widen `window_start/window_end` (min/max) and
   fill username/title if empty; else insert. Remember the mapping
   `(source_db, source_channel_id) -> unified_channel_id`.
3. **calls (sol):** copy every legacy column 1:1, `chain='sol'`, `engine='legacy'`, 7d columns NULL
   except: `max_multiple = peak_multiple`, `max_price_usd = peak_price_usd`.
4. **calls stoploss fold:** for each legacy `stoploss_results` row, write its values into the
   matching call's `sl_*` columns (join on legacy `call_id` → new call id via an id map kept in
   step 3).
5. **calls (robinhood):** copy from sim DBs with the channel id map; `chain='robinhood'`,
   `engine='7d'`; map: `entry_price_usd`, `peak_price_usd→max_price_usd`, `peak_timestamp`,
   `peak_profit_pct`, `peak_multiple = peak/entry`, `status = plain_status`,
   `is_win = (plain_status='win')`, `sl_*` from `stoploss_status`/`which_first`
   (`sl_hit_stoploss = (stoploss_status='loss' AND which_first='minus50')`,
   `sl_stoploss_timestamp` NULL (not stored in RH schema)),
   `which_threshold_first = which_first`, `api_requests_used`,
   `granular_analysis_required = (api_requests_used > 1)`,
   `target_2x_reached = (plain_status='win')`, `minus_50_reached = (which_first IN ('minus50','same_candle')) OR sl_hit_stoploss`,
   `time_2x_reached`/`time_minus_50_reached`: recompute OFFLINE from the sim DB's `price_cache`
   candles when present (walk cached minute+hour candles per the frozen spec path; zero API
   calls), else NULL; `max_price_usd`/`min_price_usd`/`max_drawdown_pct`: likewise recompute from
   cache when present (min is not stored in the RH schema), else min/drawdown NULL.
   `evaluation_end_timestamp = call_ts + 7d`. `note` = RH `note`.
6. **token_meta:** legacy rows → `chain='sol'`; sim rows → `chain='robinhood'`.
7. **price_cache:** legacy rows → `aggregate='minute'`; sim rows keep their stored `aggregate`;
   ALSO port `test_eval.db::eval_candles` (aggregate as stored) for solana pools — saves re-fetching
   during Workstream C validation. Dedup on PK (INSERT OR IGNORE).
8. **ingestion_runs:** port legacy rows with `chain='sol'`.
9. **Reconciliation report (printed, and saved to `migration_report.txt`):** per source DB and per
   table: source row count vs ported row count; per channel: calls count + win/loss totals per
   strategy before vs after; **stoploss view round-trip**: for every legacy call id, compare the
   view's row (entry, peak, peak_ts, pct, hit, sl_ts, is_win, status) against the source
   `stoploss_results` row — must be equal; print `VIEW ROUND-TRIP: OK (n rows)` or a diff list.

**Verify:** run once; eyeball report; counts match sources exactly (no drops).

**Commit:** `feat(scripts): one-shot migration to unified kolfi.db + reconciliation report`

### Task B3: Chain-aware analysis functions

**Files:**
- Modify: `analysis/windowed.py` — add optional `chain: str | None = None` parameter to
  `channel_stats_window`, `channel_buckets`, `current_streak`; when not None append
  `AND cal.chain = ?` (+param). `None` = all chains (merged). `_strategy_join`/`_win_col` unchanged
  (the view makes stoploss work as before).
- Create: `analysis/tiers.py`:

```python
TIERS = (100, 50, 25, 15, 10, 5, 2)   # plus the '<2x' complement

def tier_counts(rows, strategy: str) -> dict:
    """rows: dicts with 'multiplier' + status columns. Decided = status in (win, loss)
    for the chosen strategy ('normal' -> calls.status, 'stoploss' -> sl_status).
    multiplier = COALESCE(max_multiple, peak_multiple).
    Cumulative: count(t) = #decided with multiplier >= t; '<2x' = #decided with multiplier < 2.
    Returns {'total_decided', 'wins', 'win_rate',
             'tiers': [{'tier': '100x', count, pct}, ..., {'tier': '<2x', count, pct}],
             'granular_calls', 'avg_api_requests'}."""
```

- Tests: `tests/test_tiers.py` — cumulative invariant
  `c[100] <= c[50] <= c[25] <= c[15] <= c[10] <= c[5] <= c[2]`, `c[2] + c['<2x'] == total_decided`,
  pct math, NULL-multiplier rows counted in total but in no tier.

**Commit:** `feat(analysis): chain filter + cumulative tier counts`

### Task B4: Legacy-reader smoke on kolfi.db

**Steps (commands, no code changes expected):**
1. `DB_PATH=kolfi.db .venv/bin/python -m streamlit run ui/app.py` (or just import the view modules
   headless): open the Stop-Loss tab and Leaderboard (-50%) — they must render the same numbers as
   against `solana_tracker.db`.
2. Headless diff: run `analysis/stoploss.py` + `analysis/windowed.py` functions against both DBs
   for every channel/window/strategy; assert identical outputs (script
   `scripts/verify_migration_reads.py`, prints `READ PARITY: OK`).

**Commit:** `test(db): legacy readers produce identical output on kolfi.db`

---

## Workstream C — Engine switch in the pipeline

### Task C1: Config flags

**Files:**
- Modify: `config.py` (add to `Settings`)

```python
pricing_engine: str = field(default_factory=lambda: os.getenv("PRICING_ENGINE", "legacy"))  # legacy|7d
eval_days: int = field(default_factory=lambda: _get_int("EVAL_DAYS", 7))
entry_grace_minutes: int = field(default_factory=lambda: _get_int("ENTRY_GRACE_MINUTES", 5))
```

**Commit:** `feat(config): PRICING_ENGINE / EVAL_DAYS / ENTRY_GRACE_MINUTES`

### Task C2: Dispatcher + apply function + swap the three pricing call-sites

**Files:**
- Modify: `pipeline.py`

Add:

```python
def apply_eval7d(call_id: int, r) -> None:
    """UPDATE the calls row: status/is_win/peak_* = PLAIN result; sl_* = stoploss result;
    inline 7d columns = r fields 1:1; engine='7d'. Upsert-safe single UPDATE."""

def eval7d_to_legacy_pair(r) -> tuple[BacktestResult, StoplossResult]:
    """Mapping contract (keeps every existing consumer working):
    BacktestResult: entry=r.entry_price_usd, peak=r.max_price_usd,
      peak_timestamp=r.time_2x_reached, peak_profit_pct=(r.max_multiple-1)*100,
      is_win=(r.status_plain=='win'), status=r.status_plain, pool=r.pool_address.
    StoplossResult: entry=r.entry_price_usd, peak=r.max_price_usd,
      peak_timestamp=r.time_2x_reached, peak_profit_pct same as above,
      hit_stoploss=(r.status_stoploss=='loss' and r.minus_50_reached),
      stoploss_timestamp=r.time_minus_50_reached, is_win=(r.status_stoploss=='win'),
      status=r.status_stoploss.
    Docstring must state: for stoploss LOSSES the reported peak is the full-window peak
    (same convention legacy uses for wins); the stop-out moment is in stoploss_timestamp."""

def price_one_call(chain, client, client_v2, token_address, call_ts):
    """-> (BacktestResult, StoplossResult, Eval7dResult|None).
    engine='legacy': today's backtest_call (sol) — robinhood chain has no legacy path (error).
    engine='7d': resolve pool per chain (sol: backtest.get_cached_pool / client.resolve_pool_smart;
      robinhood: vendored DexScreener resolver), build cached fetchers with client_v2 on the
      chain's network, evaluate_call_7d, return mapped pair + raw result."""

def maturity_hours() -> float:
    return settings.eval_days * 24 if settings.pricing_engine == "7d" else settings.peak_window_hours
```

Guard: `price_one_call` with engine='7d' first checks the DB has the `engine` column
(`PRAGMA table_info(calls)`); if missing → raise with message
`"7d engine requires the unified schema — set SCHEMA_FILE=schema_unified.sql"`.

Swap call-sites (3): `run_backfill` loop (~477), `reprice_calls` loop (~624),
`mature_pending_calls` loop (~199): use dispatcher; when the 3rd element is not None call
`apply_eval7d(row["id"], res7d)`. Birdeye fallback stays but executes ONLY when engine='legacy'.
Replace the three `timedelta(hours=settings.peak_window_hours)` maturity guards with
`timedelta(hours=maturity_hours())`.

**Verify:** `PRICING_ENGINE=legacy` + `DB_PATH=<copy of kolfi.db>`: reprice a channel; statuses
identical to a pre-change run on the same copy.

**Commit:** `feat(pipeline): engine dispatcher (legacy|7d), chain-aware pool resolution`

### Task C3: Shadow validation run (Solana, on a kolfi.db copy)

1. `cp kolfi.db /tmp/shadow7d.db`
2. Pick one sol channel with ≥20 decided calls; on the COPY:
   `sqlite3 /tmp/shadow7d.db "UPDATE calls SET status='pending', engine='legacy' WHERE channel_id=<id> AND chain='sol'"`
3. `DB_PATH=/tmp/shadow7d.db SCHEMA_FILE=schema_unified.sql PRICING_ENGINE=7d .venv/bin/python`
   → `pipeline.reprice_calls(<id>, statuses=['pending'])` with a printing progress cb.
4. Compare plain win rate vs the legacy numbers for that channel; request efficiency from
   `SELECT AVG(api_requests_used), SUM(granular_analysis_required) FROM calls WHERE engine='7d'`.
5. Acceptance: plain win rate within ±1 call of legacy-7d-equivalent (7d ⊇ 12h so wins may only
   increase vs legacy-12h), avg requests/call ≤ 2.0, zero exceptions, zero 429s in logs.
   Record numbers in the commit message.

**Commit:** `chore(validation): shadow 7d run on <channel>`

### Task C4: Flip default — manual only

No code change. Document in `config.py` comment that `PRICING_ENGINE=7d` is the validated setting;
the user flips it in `.env` when ready. Never auto-flip.

---

## Workstream D — Dual-chain ingestion (one backfill prices both chains)

### Task D1: Vendored Robinhood plumbing

**Files:**
- Create: `chains/__init__.py`, `chains/robinhood_impl/__init__.py` (header:
  `# Vendored snapshot of ../robinhood_tracker @ 2026-09-12 (fetch/parse/resolve only). Do not edit the source project.`)
- Copy VERBATIM (adjusting only import lines to relative): `parser.py`, `dexscreener.py`,
  `rate_limiter.py`, `ingestion/telethon_fetcher.py` (RH's backward-window walk fix lives here).
  Do NOT vendor `strategy.py` (the shared engine replaces it), `db.py`, `config.py` (unified DB +
  main config are used), `pipeline.py` (rewritten as D2), `geckoterminal.py` (GeckoTerminalClientV2
  with `network='robinhood'` replaces it — Task A1 verifies the param).

**Verify:** `.venv/bin/python -c "from chains.robinhood_impl import parser, dexscreener"`; run the
parser on the known bot-update message (id 46 text) → 0 calls (regression guard from the RH fix).

**Commit:** `feat(chains): vendored robinhood parser/resolver/limiter`

### Task D2: `run_backfill` prices both chains in one run

**Files:**
- Modify: `pipeline.py::run_backfill`

New flow (fetch happens ONCE, chain-agnostic):

1. FETCH — existing `fetch_window_sync(channel_ref, …)` unchanged.
2. PARSE TWICE over the same message list:
   - `sol_parsed` via `ingestion/address_parser.parse_message` + existing `deduplicate_calls`.
   - `rh_parsed` via `chains.robinhood_impl.parser.parse_message` + the vendored dedup.
     The two parsers cannot collide (base58 alphabet excludes `0`).
3. ENSURE CHANNEL once (merged row, user decision #2): existing `ensure_channel(...)`; both chains'
   calls reference this single `channel_id`.
4. PERSIST + PRICE sol calls exactly as today (pending → dispatcher C2), `chain='sol'`.
5. PERSIST + PRICE robinhood calls: insert with `chain='robinhood'`, `status='pending'`, then price
   via dispatcher (7d engine; vendored DexScreener resolver; network='robinhood');
   `apply_eval7d` on success.
6. RECONCILE: existing `reconcile_channel_duplicates` / `reconcile_by_resolved_identity` must gain a
   `chain` filter (`WHERE channel_id=? AND chain='sol'`) so sol dedup never sees RH rows; add the
   equivalent RH dedup rule (same resolved-pool logic, `chain='robinhood'`) as
   `reconcile_by_resolved_identity(chain='robinhood')`.
7. PROGRESS: `Progress.found = sol_found + rh_found`; priced/unpriceable summed; stage labels gain
   a chain suffix only when both chains produced calls (e.g. `price (sol 12/20, rh 3/4)`) so the
   Streamlit + SSE consumers keep working.
8. `ingestion_runs` row as today with `chain='sol'` plus a second row `chain='robinhood'` when RH
   calls were processed.

A channel with zero calls on one chain writes nothing for that chain. A Solana-only channel
behaves byte-identically to today.

**Verify:** shadow kolfi copy + `run_backfill("@x666calls", 7d window)`: base58 calls land as
`chain='sol'`, 0x calls as `chain='robinhood'`, from the SAME run; per-chain counts match parse totals.

**Commit:** `feat(pipeline): one backfill prices solana + robinhood calls`

### Task D3: `reprice_calls` / `mature_pending_calls` chain-aware

Both already loop over `calls` rows; add `chain` to the SELECT and pass it into
`price_one_call(chain, …)`. Immature guard uses `maturity_hours()`.

**Commit:** `feat(pipeline): chain-aware reprice + maturation`

---

## Workstream E — API layer: chain param + tiers endpoint

### Task E1: `?chain=` on list endpoints

**Files:**
- Modify: `api/server.py` — `channels()`, `leaderboard()`

`chain = request.query_params.get("chain", "sol")`, accept `sol|robinhood|all`. Implement by
passing `chain=None if chain=='all' else chain` into the `analysis/windowed.py` functions (B3) and
tagging each output row with `"chain"`. For `all`, leaderboard ranks over the merged row list.
`sol` must produce JSON identical to today (diff curl snapshots before/after; paste diff in commit).

### Task E2: `?chain=` on deep-dive endpoints

`channel_detail`, `channel_buckets`, `channel_streak`, `channel_calls`: accept `chain`
(`all` ALLOWED here — merged stats for one channel, user decision #2 makes this meaningful).
`_resolve` unchanged (channel rows are chain-agnostic). `channel_calls` rows gain
`chain`, `which_first`, `api_requests_used`, `granular`, `max_drawdown_pct`,
`multiplier = COALESCE(max_multiple, peak_multiple)`.

### Task E3: NEW `/api/channels/{handle}/tiers`

Query: `chain` (sol|robinhood|all, default sol), `strategy` (normal|stoploss), `window`
(1d|7d|1m|3m|all) and optional `days` (1|3|7|30 — the card's pills; **`days` wins over `window`**).
Handler: fetch decided rows for the channel+chain+strategy+effective-since via one SQL
(`since = now - days` when `days` set, else `W.window_since(window)`), then
`analysis/tiers.tier_counts(...)`. Response:

```json
{
  "scope": {"chain": "sol", "strategy": "stoploss", "since": "2026-08-13T00:00:00Z"},
  "total_decided": 22, "wins": 12, "win_rate": 54.5,
  "tiers": [
    {"tier": "100x", "count": 2,  "pct": 9.1},
    {"tier": "50x",  "count": 2,  "pct": 9.1},
    {"tier": "25x",  "count": 5,  "pct": 22.7},
    {"tier": "15x",  "count": 5,  "pct": 22.7},
    {"tier": "10x",  "count": 5,  "pct": 22.7},
    {"tier": "5x",   "count": 8,  "pct": 36.4},
    {"tier": "2x",   "count": 12, "pct": 54.5},
    {"tier": "<2x",  "count": 10, "pct": 45.5}
  ],
  "granular_calls": 9, "avg_api_requests": 1.66
}
```

Register route in `app.routes`. Tiers include legacy rows (multiplier from `peak_multiple`) —
user decision #3; NO caption field, NO engine annotation.

### Task E4: `?chain=` on fetch endpoints

`fetch_stream` / `fetch_channels`: body gains optional `"chain"` (default sol) — for robinhood,
resolve/insert the merged channel row then price via dispatcher with chain='robinhood'; same SSE
event shape.

**Commit per task** (E1..E4).

---

## Workstream F — Frontend (Next.js)

### Task F1: Store + types

- `frontend/src/types/index.ts`: `export type Chain = "sol" | "robinhood" | "all";`
- `frontend/src/store/uiStore.ts`: add `chain: Chain` (default `"sol"`), `setChain`,
  `deepDiveChain: Chain` (default `"sol"`), `setDeepDiveChain`.

### Task F2: ChainToggle + Navbar

- Create `frontend/src/components/ChainToggle.tsx`: pill group `[SOL | RH | ALL]` styled exactly
  like `StrategyToggle.tsx` (rounded-full border, gold active bg). Tooltip:
  "Chain: Solana / Robinhood / both".
- `Navbar.tsx`: render `<ChainToggle />` immediately LEFT of `<StrategyToggle />`.

### Task F3: api.ts chain plumbing

- Every getter gains `chain` → `&chain=${chain}`; new `ApiTier`, `ApiTiersResponse` interfaces;
  new `tiers(handle, chain, strategy, daysOrNull, window)`.
- `page.tsx`, `leaderboard/page.tsx`, `tokens/page.tsx`: read `chain` from store, pass through;
  when `chain === "all"` show a small chain badge on each card/row (`SOL` / `RH`, 10px muted).
- Channel card click: `setDeepDiveChain(row.chain === undefined ? chain : row.chain)` before
  navigating to `/channels/[handle]` (under `all`, a card's own chain wins).

### Task F4: PerformanceRanking component (the screenshot, faithfully)

Create `frontend/src/components/PerformanceRanking.tsx`:

- Card container: existing `.card p-6`.
- Header row: left two-line bold title `Performance` / `Ranking` (`text-2xl font-bold leading-tight`);
  right: day pills `1 3 7 30` (plain text buttons; active = white bold, inactive muted). Local
  state `days: 1|3|7|30|null`; **`null` = follow the global TimeFilter** (user decision #3: the
  meter tracks the selected timeframe; a pill overrides it).
- Meter row: `grid grid-cols-8 gap-3`. Each column, top→bottom:
  1. Stack of 4 horizontal bars (`h-2.5 rounded-sm`, gap-1), lit bottom-up:
     `lit = count === 0 ? 0 : max(1, ceil(count / maxCount * 4))` where `maxCount` = max count over
     the 8 tiers in the current response. Lit color `var(--accent-teal)`; for the `<2x` column lit
     color muted red `rgb(185 28 28)`; unlit `rgba(255,255,255,0.06)`.
  2. Tier label bold white `text-sm font-bold`: `X100 X50 X25 X15 X10 X5 X2 <X2`.
  3. Percent `text-sm text-[var(--text-secondary)]` (e.g. `9%`).
  4. Count in parens muted: `(2)`.
- Summary row (3 left-aligned columns): captions `Calls` / `Wins` / `Win Ratio`
  (`text-sm text-[var(--text-muted)]`) over big values (`text-2xl font-bold`): `total_decided`,
  `wins`, `win_rate%`.
- Data: `api.tiers(handle, deepDiveChain, apiStrategy(strategy), days, timeWindow)`; refetch on
  `[handle, deepDiveChain, strategy, timeWindow, days]`.
- Loading: skeleton bars — never fake numbers. Empty: `total_decided === 0` → muted
  "No decided calls in this window".
- NO caption line about window/engine anywhere (user decision #3).

### Task F5: Deep-dive page integration

`frontend/src/app/channels/[handle]/page.tsx`:
- use `deepDiveChain` for ALL api calls (detail/buckets/calls/tiers);
- stats bar appends `· {chain label}`;
- render `<PerformanceRanking />` between the stats bar and the buckets card;
- header badge shows the chain when not `sol`.

### Task F6: Visual verification

`cd frontend && npm run build` passes; dev server + curl the API; screenshot deep-dive with
`deepDiveChain=robinhood, strategy=50, window=3m` and compare to the mockup by eye; then switch
TimeFilter 3m→7d and confirm the meter counts change (window tracking works).

**Commit per task** (F1..F6).

---

## Workstream G — Robinhood historical backfill (gated on explicit user approval)

**Scope (user decision):** Robinhood ONLY, from the standard anchor
`pipeline.preset_window("5m")` (first day of the month 5 months back; 2026-04-01 when run in
September 2026; tracks the month automatically). End = now. No Solana historical re-pricing.

**Files:**
- Create: `scripts/backfill_robinhood_history.py`

Behaviour:
1. `window_start, window_end = preset_window("5m")` — import from `pipeline`, never recompute.
2. For each channel in `kolfi.db` that has `chain='robinhood'` calls (or is requested explicitly),
   fetch + parse + price via the D2 machinery (`run_backfill(ref, window_start, window_end)` with
   the RH path); dedup keys make it idempotent against the migrated rows.
3. Rate-limited by the shared token bucket; prints live real counters (`priced X/Y, reqs, ETA`).
4. Prints a cost estimate BEFORE fetching (`expected calls × ~1.6 req ÷ 5 RPM ≈ minutes`) and
   requires `--yes`. Never auto-runs; never touches `solana_tracker.db`.
5. Writes to `kolfi.db` only.

**Acceptance:** `/api/leaderboard?chain=robinhood` all-time numbers reconcile with the validated
sim results (666: 52.9% plain / 41.2% stoploss; Oliver: 62.5% / 50.0%) within the calls the wider
window adds; `avg(api_requests_used) ≤ 2.0`.

**Commit:** `feat(scripts): robinhood historical backfill from standard 5m anchor`

---

## Cutover & rollback (the only moment the live app changes)

1. Run B2 migration → review `migration_report.txt` (counts + `VIEW ROUND-TRIP: OK`).
2. Run B4 read-parity → `READ PARITY: OK`.
3. User eyeballs both → add to `.env`: `DB_PATH=kolfi.db` and `SCHEMA_FILE=schema_unified.sql`.
4. Restart API + frontend; smoke: channels/leaderboard/deep-dive/stop-loss tabs render.
5. Rollback at any time: delete those two `.env` lines, restart. `solana_tracker.db` was never
   written, so nothing is lost.

## Verification checklist (definition of done)

1. `pytest tests/` green (A2 tiers-invariant tests, B3 tier tests, C2 mapping tests).
2. `scripts/parity_7d.py` → `MISMATCHES: 0`.
3. Migration report: source vs ported counts equal per table; `VIEW ROUND-TRIP: OK`.
4. `scripts/verify_migration_reads.py` → `READ PARITY: OK` (legacy readers identical on kolfi.db).
5. `PRICING_ENGINE=legacy` smoke on kolfi.db: API JSON for `chain=sol` byte-identical to pre-change
   snapshots of `/api/channels`, `/api/leaderboard`, `/api/channels/<h>`, `.../buckets`, `.../calls`.
6. `PRICING_ENGINE=7d` shadow run (C3) numbers recorded; `solana_tracker.db` untouched
   (`sqlite3 solana_tracker.db "PRAGMA integrity_check"` + row counts unchanged).
7. Dual-chain backfill (D2): one run writes `chain='sol'` and `chain='robinhood'` rows; a
   Solana-only channel's rows unchanged vs pre-change.
8. `?chain=robinhood` leaderboard returns the validated RH numbers; `?chain=all` merges + re-ranks
   with a `chain` tag per row; deep-dive works for sol / robinhood / all.
9. `/api/channels/<h>/tiers`: cumulative property holds; changing `window` (or `days`) changes the
   counts; legacy sol rows included via peak_multiple.
10. Frontend: `npm run build` clean; meter renders 8 columns, bottom-up lit bars, red `<2x`,
    follows TimeFilter, day pills override; ChainToggle drives every page.
11. No new deps in `package.json` / `requirements.txt`.
12. Workstream G (when approved): RH all-time numbers reconcile; solana DB still untouched.

## Risks & open questions

- **Birdeye under 7d:** no Birdeye path for the 7d engine; unpriceable sol tokens stay unpriceable
  (legacy engine keeps the fallback).
- **RH min_price/drawdown on migrated rows:** recomputed offline from the sim DBs' cached candles;
  NULL where candles are missing (honest blanks, same as the Oliver dump).
- **GT budget for G:** ~41 known RH calls + wider-window additions ≈ 70–90 requests ≈ 15–20 min at
  5 RPM. Gated on `--yes`.
- **Streamlit app untouched:** it keeps working via the view but shows Solana-only, legacy-style
  views; all new UI is the Next.js frontend.
- **`peak_timestamp` under 7d** carries `time_2x_reached` (not the true peak candle ts); consumers
  only display it in the stop-loss table's "SL Time"-adjacent columns — acceptable, documented in
  the mapping docstring.
