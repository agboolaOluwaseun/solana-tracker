# Solana Caller Tracker

A KOLfi-style Streamlit dashboard that ingests **historical** Telegram channel
data over a fixed 12-week window, extracts Solana token "calls," backtests each
call's peak ROI via GeckoTerminal, and ranks **channels** by win rate.

> **Win rule:** a call is a **WIN** iff the token's peak price within **24h**
> after the call reaches **≥ 2x** the entry price (the price at the call time).
> Un-priceable calls (no pool / no OHLCV / rugged) count as **losses**, so the
> denominator is every call in the channel.

This is a **historical-only** tool — it does **not** monitor Telegram live.

---

## How it works

```
Telegram channel ──fetch──▶ parse Solana mints ──▶ resolve pool (GeckoTerminal)
   (Telethon, last 12w)                                │
                                                       ▼
                       leaderboard / deep-dive / call log  (Streamlit)
                                       ▲
                          score ◀── fetch 24h OHLCV (GeckoTerminal, cached)
                     entry = call-time price, peak = max high
```

1. **Fetch** — Telethon pulls every message in the channel over `[start, start+12w)`.
2. **Parse** — a base58 regex extracts Solana mint addresses; the first address
   per message becomes a "call".
3. **Resolve** — GeckoTerminal maps each token → its highest-liquidity Solana pool.
4. **Price** — GeckoTerminal OHLCV gives the hourly candles for 24h after the
   call; entry = first candle open, peak = max high. Every candle is cached, so
   re-runs cost **zero** API calls.
5. **Score & persist** — each call is committed as soon as it's priced
   (→ **resumable**; a crash picks up from the last pending call).

---

## Setup

### 1. Install dependencies

```bash
cd solana_tracker
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Then edit `.env`:

| Var | Required | Description |
|-----|----------|-------------|
| `TELEGRAM_API_ID` | ✅ | From https://my.telegram.org → API development tools |
| `TELEGRAM_API_HASH` | ✅ | Same |
| `SESSION_NAME` | – | Telethon session file name (default `solana_tracker`) |
| `TELEGRAM_PHONE` | – | Phone in intl format; speeds up the first login |
| `GECKOTERMINAL_API_KEY` | – | Paid GeckoTerminal key → bumps throttle to ~250 RPM |
| `PEAK_WINDOW_HOURS` | – | Default `24` |
| `WIN_MULTIPLIER` | – | Default `2.0` |
| `WINDOW_WEEKS` | – | Default `12` |

### 3. First-time Telegram login

The first backfill starts a Telethon session and will **interactively** ask for
your phone number + the login code Telegram sends you (and your 2FA password if
enabled). This happens once; the session file (`<SESSION_NAME>.session`) is
reused after that.

---

## Usage

### Option A — Streamlit dashboard

```bash
streamlit run ui/app.py
```

Use the sidebar to enter a channel (`@username`), pick the window start date,
and click **Run backfill**. Progress streams inline. Then browse the three tabs:

- **🏆 Leaderboard** — channels ranked by win rate, with avg/best peak profit + CSV export.
- **🔎 Channel Deep-Dive** — per-channel Week 1–12 success-rate bar chart vs. overall line.
- **📋 Call Log** — every call (token, date/time, entry→peak, profit %, result), filterable + CSV.

### Option B — CLI

```bash
python -m scripts.backfill --channel @some_channel --start 2026-03-15
# add --limit 5000 to cap scanned messages, --weeks 12 to override window length
```

---

## Tuning the win rule

Edit `.env` (or pass values) and re-run — re-runs are cheap because of the
price cache:

- `WIN_MULTIPLIER=2.0` — minimum multiple for a win (2.0 = +100%).
- `PEAK_WINDOW_HOURS=24` — how long after the call to look for the peak.

---

## Project layout

```
solana_tracker/
  config.py            env + settings (window, 2x, 24h, RPM)
  db.py                SQLite connection (thread-local, WAL)
  schema.sql           tables: channels, calls, token_meta, price_cache, ingestion_runs
  models.py            dataclasses (RawMessage, ParsedCall, PricePoint, BacktestResult, WeekBuckets)
  ingestion/
    telethon_fetcher.py  iter_messages over the fixed window
    address_parser.py    Solana mint extraction (first address per message)
  pricing/
    rate_limiter.py      sliding-window throttle (free 28 / paid 240 RPM)
    cache.py             price_cache table read/write
    geckoterminal.py     pool resolution + OHLCV fetch (tenacity backoff)
    backtest.py          score one call (entry, peak, is_win)
  analysis/
    weekly.py            per-week + overall win rates
    leaderboard.py       channel ranking
  pipeline.py           resumable fetch→parse→price→score→persist
  scripts/backfill.py   CLI
  ui/
    app.py              Streamlit entry (sidebar + tabs)
    views/
      leaderboard.py
      channel_deep_dive.py
      call_log.py
```

---

## Notes & limitations

- **Rate limits:** without a GeckoTerminal key the run is throttled to ~28 RPM.
  It's slow but reliable — every candle is cached so you only pay once per token.
- **Channel-only model:** there's no per-caller tracking; the leaderboard and
  deep-dive are per-channel. To attribute calls to a specific person inside a
  shared channel, that would require sender parsing (not implemented here).
- **First-address policy:** if a message contains multiple Solana mints, only
  the first is treated as the call.
- **Telethon window:** `iter_messages(offset_date=window_end, reverse=True)` walks
  forward from the window end; messages predating `window_start` stop the scan.

---

## Troubleshooting

- **`RuntimeError: Telegram not configured`** — set `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` in `.env`.
- **Empty leaderboard** — run a backfill first; check the sidebar's status output.
- **Many `unpriceable_loss` rows** — common for low-cap/rugged tokens with no
  GeckoTerminal pool or OHLCV history. These count as losses by design.
- **`No module named ...`** — run commands from the `solana_tracker/` directory so
  the package root is on `sys.path`.
