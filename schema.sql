-- solana_tracker database schema (SQLite)
-- Channel-only model: there is no per-caller tracking. The unit of analysis
-- is the Telegram channel.

CREATE TABLE IF NOT EXISTS channels (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_channel_id INTEGER NOT NULL,
    username            TEXT,                       -- e.g. "some_channel" (no @)
    title               TEXT,
    window_start        TEXT NOT NULL,              -- ISO8601 UTC
    window_end          TEXT NOT NULL,              -- ISO8601 UTC
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(telegram_channel_id)
);

CREATE TABLE IF NOT EXISTS token_meta (
    address       TEXT PRIMARY KEY,                -- Solana mint address
    symbol        TEXT,
    name          TEXT,
    dex_pool_id   TEXT,
    liquidity_usd REAL,
    first_seen    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS calls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id        INTEGER NOT NULL REFERENCES channels(id),
    message_id        INTEGER NOT NULL,
    raw_text          TEXT,
    token_address     TEXT NOT NULL,
    token_symbol      TEXT,
    token_name        TEXT,
    call_timestamp    TEXT NOT NULL,               -- ISO8601 UTC of the Telegram message
    week_index        INTEGER NOT NULL,            -- 1..window_weeks
    pool_address      TEXT,
    entry_price_usd   REAL,
    peak_price_usd    REAL,
    peak_timestamp    TEXT,                        -- ISO8601 UTC of the peak candle
    peak_profit_pct   REAL,                        -- (peak/entry - 1) * 100
    is_win            INTEGER NOT NULL DEFAULT 0,  -- 1 = win (peak >= WIN_MULTIPLIER * entry)
    status            TEXT NOT NULL DEFAULT 'pending',  -- pending|win|loss|unpriceable_loss
    priced_at         TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(channel_id, message_id, token_address)
);

CREATE INDEX IF NOT EXISTS idx_calls_channel ON calls(channel_id);
CREATE INDEX IF NOT EXISTS idx_calls_channel_week ON calls(channel_id, week_index);
CREATE INDEX IF NOT EXISTS idx_calls_token ON calls(token_address);
CREATE INDEX IF NOT EXISTS idx_calls_status ON calls(status);
CREATE INDEX IF NOT EXISTS idx_calls_timestamp ON calls(call_timestamp);

CREATE TABLE IF NOT EXISTS price_cache (
    pool_address   TEXT NOT NULL,
    token_address  TEXT NOT NULL,
    candle_ts      TEXT NOT NULL,                  -- ISO8601 UTC of candle start
    open           REAL,
    high           REAL,
    low            REAL,
    close          REAL,
    volume         REAL,
    source         TEXT NOT NULL DEFAULT 'geckoterminal',
    cached_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (pool_address, token_address, candle_ts)
);

CREATE INDEX IF NOT EXISTS idx_price_cache_token ON price_cache(token_address);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id        INTEGER NOT NULL REFERENCES channels(id),
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    mode              TEXT NOT NULL,               -- full|resume
    messages_scanned  INTEGER NOT NULL DEFAULT 0,
    calls_found       INTEGER NOT NULL DEFAULT 0,
    calls_priced      INTEGER NOT NULL DEFAULT 0,
    calls_unpriceable INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'running',  -- running|completed|failed
    error             TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS stoploss_results (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id           INTEGER NOT NULL REFERENCES calls(id),
    entry_price_usd   REAL,
    peak_price_usd    REAL,
    peak_timestamp    TEXT,
    peak_profit_pct   REAL,
    hit_stoploss      INTEGER NOT NULL DEFAULT 0,
    stoploss_timestamp TEXT,
    is_win            INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'pending',
    error             TEXT,
    computed_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(call_id)
);
