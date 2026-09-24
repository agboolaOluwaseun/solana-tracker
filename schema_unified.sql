-- Unified KOLfi schema (kolfi.db) — Solana + Robinhood chain, 7d engine fields inline.
-- Authoritative spec: .hermes/plans/2026-09-12_0100-integration-7d-engine-chains-tiers.md (Task B1).
-- One-shot migration in scripts/migrate_to_unified_db.py; legacy solana_tracker.db stays untouched.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS channels (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_channel_id INTEGER NOT NULL UNIQUE,
    username            TEXT,
    title               TEXT,
    window_start        TEXT NOT NULL,
    window_end          TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    -- Scan checkpoint: UTC ISO ('Z') of the newest message timestamp THIS
    -- channel's messages were last walked through (regardless of whether a
    -- call was found). Opportunistic refresh anchors here instead of at
    -- MAX(call.timestamp) — otherwise a quiet channel re-scans its entire
    -- silent backlog on every single boot (user report 2026-09-16: loading
    -- bars 'since 26 Aug' for channels that simply posted no new calls).
    last_scanned_at     TEXT
);   -- chain-agnostic: ONE row per Telegram channel (merged; chain lives on calls)

CREATE TABLE IF NOT EXISTS token_meta (
    chain         TEXT NOT NULL,            -- 'sol' | 'robinhood'
    address       TEXT NOT NULL,
    symbol        TEXT,
    name          TEXT,
    dex_pool_id   TEXT,
    liquidity_usd REAL,
    quote_symbol  TEXT,            -- the paired side's ticker (orientation guard)
    pool_is_base  INTEGER,         -- 1 called token holds GT's base seat;
                                   -- 0 flipped (curve is the OTHER asset);
                                   -- NULL never checked
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
    scored_window       TEXT NOT NULL DEFAULT '7d',
    -- legacy pricing fields (plain strategy semantics, as today)
    pool_address      TEXT,
    entry_price_usd   REAL,
    peak_price_usd    REAL,
    peak_timestamp    TEXT,
    peak_profit_pct   REAL,
    peak_multiple     REAL,
    is_win            INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'pending',   -- pending|win|loss|unpriceable_loss|excluded
    pending_reason    TEXT,
    score_state       TEXT NOT NULL DEFAULT 'final',     -- live=provisional (7d window still open), final=closed
    priced_at         TEXT,
    -- legacy extra columns kept 1:1 so existing writers keep working post-cutover
    error                 TEXT,          -- written by pricing/api_wrappers.py persist path
    pre_target_drawdown_frac REAL,       -- legacy diagnostic field
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
CREATE INDEX IF NOT EXISTS idx_calls_channel   ON calls(channel_id);
CREATE INDEX IF NOT EXISTS idx_calls_chain     ON calls(chain);
CREATE INDEX IF NOT EXISTS idx_calls_status    ON calls(status);
CREATE INDEX IF NOT EXISTS idx_calls_timestamp ON calls(call_timestamp);

-- Aggregate-aware candle cache: 'minute' and 'hour' rows coexist; PK includes aggregate.
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
    started_at TEXT NOT NULL,
    finished_at TEXT,
    mode TEXT NOT NULL,
    messages_scanned INTEGER NOT NULL DEFAULT 0,
    calls_found INTEGER NOT NULL DEFAULT 0,
    calls_priced INTEGER NOT NULL DEFAULT 0,
    calls_unpriceable INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- stoploss_results: REAL table, identical DDL to legacy schema.sql.
-- Why not a view (plan v2 B1 said view): SQLite rejects "cannot UPSERT a view"
-- and the pipeline's persist_stoploss_result / reprice writers all use
-- INSERT ... ON CONFLICT(call_id) DO UPDATE. A real table keeps every legacy
-- reader AND writer working unedited; RH stoploss data folds in as plain rows.
CREATE TABLE IF NOT EXISTS stoploss_results (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id           INTEGER NOT NULL REFERENCES calls(id),
    entry_price_usd   REAL,
    peak_price_usd    REAL,
    peak_timestamp    TEXT,
    peak_profit_pct   REAL,
    hit_stoploss      INTEGER NOT NULL DEFAULT 0,
    stoploss_timestamp TEXT,
    final_close_usd   REAL,           -- mark-to-market for 'expired' (window
    final_close_ts    TEXT,           -- ended, stop never triggered)
    is_win            INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'pending',   -- win|loss|unpriceable_loss|expired|pending
    error             TEXT,
    computed_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(call_id)
);
CREATE INDEX IF NOT EXISTS idx_sl_status ON stoploss_results(status);

-- Strategy 3: 50% TRAILING stop-loss. Stop = 50% of the running peak (never
-- falls). WIN = 2x entry reached before the trailing stop triggered; LOSS =
-- a candle low reached half the running peak first. exit_multiple = the
-- FRACTION OF CAPITAL RETURNED at the stop-out (peak/2; for STOP-OUT losses
-- always 0.5..1.0 since peak<2x — a guaranteed loss floored at 50%). 'expired'
-- (hit_trailing_stop=0) = window closed with no trigger: NOT a verdict,
-- excluded from every stat; exit_multiple/loss_pct record the final close as
-- mark-to-market for the gray tile. loss_pct = (exit_multiple-1)*100 -> e.g.
-- peak 1.8x, out 0.9x = -10%.
CREATE TABLE IF NOT EXISTS trailing_results (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id           INTEGER NOT NULL REFERENCES calls(id),
    entry_price_usd   REAL,
    peak_multiple     REAL,               -- running peak at/behind the stop-out
    peak_profit_pct   REAL,               -- (peak_multiple-1)*100 for AVG readers
    peak_price_usd    REAL,
    peak_timestamp    TEXT,
    exit_multiple     REAL,               -- peak/2 for losses (capital returned)
    exit_price_usd    REAL,
    exit_timestamp    TEXT,
    loss_pct          REAL,               -- (exit_multiple-1)*100, negative on loss
    hit_trailing_stop INTEGER NOT NULL DEFAULT 0,
    is_win            INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'pending',   -- win|loss|unpriceable_loss|expired|pending
    error             TEXT,
    computed_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(call_id)
);
CREATE INDEX IF NOT EXISTS idx_tr_status ON trailing_results(status);
