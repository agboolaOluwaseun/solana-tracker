"""
Build a fresh TEST database for the new 7-day evaluation strategy
(screening + optional minute resolution, aggregate-aware cache).

Creates test_eval.db containing ONLY row copies from the existing sim DBs:
  - channels, calls (with legacy 12h columns for comparison)
  - token_meta (source of correct pools)
  - calls_7d legacy results (for reference)
Candle data is NOT copied: the strategy fetches its own OHLCV into the
aggregate-aware `eval_candles` table.  Main DB untouched.

Usage: python scripts/build_test_db.py
"""
import sqlite3
from pathlib import Path

ROOT = Path(__file__).parent.parent
SIM_DBS = [
    ("simulation_666.db", "666"),
    ("simulation_trenches.db", "trenches"),
    ("simulation_eleetmo.db", "eleetmo"),
]
TEST_DB = ROOT / "test_eval.db"

# Fresh start
if TEST_DB.exists():
    TEST_DB.unlink()

conn = sqlite3.connect(TEST_DB, isolation_level=None)
conn.row_factory = sqlite3.Row

conn.executescript("""
CREATE TABLE channels (
    id INTEGER PRIMARY KEY,
    channel_id INTEGER,
    username TEXT,
    title TEXT,
    source_db TEXT
);
CREATE TABLE calls (
    id INTEGER NOT NULL,               -- original per-DB id (repeats across channels)
    channel_tag TEXT NOT NULL,
    source_db TEXT,
    message_id INTEGER,
    raw_text TEXT,
    token_address TEXT NOT NULL,
    token_symbol TEXT,
    token_name TEXT,
    call_timestamp TEXT NOT NULL,
    pool_address TEXT,                 -- legacy 12h pool (fallback only)
    legacy_entry_price REAL,           -- 12h/1-min corrected entry (reference only)
    legacy_peak_price REAL,
    legacy_peak_timestamp TEXT,
    legacy_peak_profit_pct REAL,
    legacy_status TEXT,                -- win|loss|unpriceable_loss (12h, corrected)
    legacy_7d_peak_mult REAL,          -- from calls_7d (hourly 7d), reference only
    legacy_7d_status TEXT,
    PRIMARY KEY (channel_tag, id),
    UNIQUE(channel_tag, message_id, token_address)
);
CREATE TABLE token_meta (
    address TEXT PRIMARY KEY,
    symbol TEXT,
    name TEXT,
    dex_pool_id TEXT,
    liquidity_usd REAL,
    channel_tag TEXT
);
CREATE TABLE eval_candles (
    pool_address TEXT NOT NULL,
    token_address TEXT NOT NULL,
    aggregate TEXT NOT NULL,         -- 'minute' | 'hour'
    candle_ts TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (pool_address, token_address, aggregate, candle_ts)
);
CREATE TABLE eval_results (
    id INTEGER NOT NULL,             -- call id within channel
    channel_tag TEXT NOT NULL,
    token_address TEXT,
    token_symbol TEXT,
    call_timestamp TEXT,
    pool_address TEXT,
    screening_entry_price REAL,
    screening_target REAL,
    actual_entry_price REAL,
    actual_target REAL,
    maximum_price_after_call REAL,
    minimum_price_after_call REAL,
    maximum_multiple REAL,
    maximum_drawdown_pct REAL,
    target_2x_reached INTEGER,
    minus_50_reached INTEGER,
    time_2x_reached TEXT,
    time_minus_50_reached TEXT,
    which_threshold_occurred_first TEXT,   -- '2x'|'minus50'|'none'|'same_candle_win'
    plain_status TEXT,               -- win|loss|unpriceable_loss
    stoploss_status TEXT,
    api_requests_used INTEGER,
    granular_analysis_required INTEGER,
    evaluation_end_timestamp TEXT,
    note TEXT,
    PRIMARY KEY (channel_tag, id)
);
""")

total_calls = 0
for db_name, tag in SIM_DBS:
    src = sqlite3.connect(ROOT / db_name)
    src.row_factory = sqlite3.Row

    # channels
    for r in src.execute("SELECT * FROM channels"):
        conn.execute(
            "INSERT OR REPLACE INTO channels (id, channel_id, username, title, source_db) VALUES (?,?,?,?,?)",
            (r["id"], r["telegram_channel_id"], r["username"], r["title"], db_name),
        )

    # token_meta
    for r in src.execute("SELECT address, symbol, name, dex_pool_id, liquidity_usd FROM token_meta"):
        conn.execute(
            "INSERT OR REPLACE INTO token_meta (address, symbol, name, dex_pool_id, liquidity_usd, channel_tag) VALUES (?,?,?,?,?,?)",
            (r["address"], r["symbol"], r["name"], r["dex_pool_id"], r["liquidity_usd"], tag),
        )

    # calls + legacy 7d
    has_7d = src.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='calls_7d'"
    ).fetchone() is not None
    for r in src.execute(
        "SELECT id, message_id, raw_text, token_address, token_symbol, token_name, "
        "call_timestamp, pool_address, entry_price_usd, peak_price_usd, peak_timestamp, peak_profit_pct, status "
        "FROM calls"
    ):
        mult_7d = None
        status_7d = None
        if has_7d:
            r7 = src.execute(
                "SELECT peak_price_usd, entry_price_usd, status FROM calls_7d WHERE id=?",
                (r["id"],),
            ).fetchone()
            if r7 and r7["entry_price_usd"] and r7["peak_price_usd"]:
                mult_7d = r7["peak_price_usd"] / r7["entry_price_usd"]
                status_7d = r7["status"]
        conn.execute(
            "INSERT OR REPLACE INTO calls (id, channel_tag, source_db, message_id, raw_text, "
            "token_address, token_symbol, token_name, call_timestamp, pool_address, legacy_entry_price, "
            "legacy_peak_price, legacy_peak_timestamp, legacy_peak_profit_pct, legacy_status, "
            "legacy_7d_peak_mult, legacy_7d_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["id"], tag, db_name, r["message_id"], r["raw_text"], r["token_address"],
             r["token_symbol"], r["token_name"], r["call_timestamp"], r["pool_address"], r["entry_price_usd"],
             r["peak_price_usd"], r["peak_timestamp"], r["peak_profit_pct"], r["status"],
             mult_7d, status_7d),
        )
        total_calls += 1
    src.close()

print(f"Built {TEST_DB}")
print(f"  channels: {conn.execute('SELECT COUNT(*) FROM channels').fetchone()[0]}")
for tag, n in conn.execute(
    "SELECT channel_tag, COUNT(*) FROM calls GROUP BY channel_tag"
):
    print(f"  calls[{tag}]: {n}")
print(f"  token_meta: {conn.execute('SELECT COUNT(*) FROM token_meta').fetchone()[0]}")
conn.close()