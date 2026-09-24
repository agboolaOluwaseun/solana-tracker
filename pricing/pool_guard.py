"""Pool-orientation guard (user lesson 2026-09-23: museic / Big Pharmai).

GeckoTerminal's OHLCV for a pool tracks the pool's BASE seat in USD. The
pool resolver (DexScreener highest-liquidity) can hand us a pair where the
called token sits in the QUOTE seat — GT then serves the *other* asset's
price curve and the engine scores that as the called token. museic was
priced on META's $750 stock curve; DRUGS on BIO's curve. Both printed
false losses on real wins.

Detection — two free tripwires, one cheap confirmation:
  1. exotic quote: the pool's quote token is not a native/stable/wrapped
     asset (museic paired against META, DRUGS against another token).
     Symbol comes from the DexScreener response we already parsed — zero
     extra calls.
  2. magnitude: the scored curve's last close vs the called token's own
     quoted price (both from responses we already hold) diverges >20x.
     Only trustworthy when the curve reference is RECENT (young/live
     window) — a months-old backfill window legitimately drifts, so the
     ratio check carries a freshness limit.
  3. on suspicion: ONE GeckoTerminal /tokens/{addr}/pools listing per pool
     (result cached forever in token_meta.pool_is_base). The listing names
     GT's actual seat for every pool containing the token — authoritative
     and, at the same time, hands us the candidate list for correction.

Correction cascade (only when a flip is CONFIRMED — <1% of calls):
  a. base-seat pool of the same token on GT with reserve >= $1000 -> score
     through the real engine on it (full 1m-entry/hourly-week discipline);
  b. none usable -> Birdeye (prices by token address; seats can't lie),
     fetch-time only, per standing policy;
  c. neither -> verdict refused: 'orientation ambiguous' (row stays
     pending/retried; never scored off a known-wrong curve).

Cost on healthy calls: ZERO extra API calls. The listing fires only for
pools a tripwire flagged, once per pool, ever.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

# Quote tokens that are "safe seats" per chain: native, wrapped native,
# or a stable. Anything else paired with the called token is exotic and
# gets the one-call seat confirmation (a token-token pair is where GT
# may have seated the bigger coin as base).
SAFE_QUOTES = {
    "sol": {"SOL", "WSOL", "USDC", "USDT", "JUP", "BONK", "TRUMP", "PYUSD",
            "SUSD", "USDH", "WIF", "RAYSOL", "JitoSOL", "mSOL", "SOSOL"},
    "robinhood": {"LIT", "WLIKED", "WLIT", "USDC", "USDG", "USDG?", "USDT",
                  "ETH", "WETH", "DEGEN", "ARB", "BASE", "WBASE", "RH"},
    "eth": {"ETH", "WETH", "USDC", "USDT", "DAI", "USDe", "PYUSD"},
    "bsc": {"BNB", "WBNB", "USDC", "USDT", "CAKE"},
    "base": {"ETH", "WETH", "USDC", "cbBTC", "DEGEN", "AERO"},
    "arc": {"USDC", "ARC", "WETH", "ETH"},
}
# A generalised fallback set so a symbol we never saw degrades to "exotic"
# (safer: pays one cached confirm call) rather than silently trusted.
GENERIC_STABLES = SAFE_QUOTES["eth"] | SAFE_QUOTES["sol"] | {
    "USDC", "USDT", "USD1", "FDUSD", "PYUSD", "DAI", "USDe", "TUSD",
    "WETH", "WBTC", "CBBTC", "WBNB", "WSOL", "WLIT", "WLIKED", "LIT",
    "STONKS", "CLANKER"}

ORIENT_RATIO = 20.0
#: max age of the curve reference for the magnitude tripwire to be trusted
#: (a live/young window's last close is hours old and must match the token's
#: quoted price; a window that closed months ago legitimately drifted)
FRESH_HOURS = 48


def is_exotic_quote(chain: str, quote_symbol: Optional[str]) -> bool:
    """True when the pool's quote token is not a known native/stable."""
    if not quote_symbol:
        return False          # unknown quote -> don't flag (no signal)
    s = quote_symbol.upper()
    internal = {"solana": "sol", "ethereum": "eth"}.get(chain, chain)
    safe = SAFE_QUOTES.get(internal, set()) | {q.upper() for q in GENERIC_STABLES}
    return s not in safe


def magnitude_suspect(curve_ref: Optional[float], ds_price: Optional[float],
                      ref_age_hours: Optional[float]) -> bool:
    """Curve-vs-quote divergence beyond ORIENT_RATIO, only when the curve
    reference is fresh enough for 'now' to be a valid comparator."""
    if not curve_ref or not ds_price or curve_ref <= 0 or ds_price <= 0:
        return False
    if ref_age_hours is not None and ref_age_hours > FRESH_HOURS:
        return False
    ratio = curve_ref / ds_price
    return not (1.0 / ORIENT_RATIO <= ratio <= ORIENT_RATIO)


def _bare(pid: str) -> str:
    """GT object ids look like 'robinhood_0x…' / 'solana_G7k…' — the raw
    address is everything after the FIRST underscore."""
    return pid.split("_", 1)[1] if "_" in pid else pid


def parse_pools(listing: Optional[dict], token_address: str, pool_address: str):
    """From a GT /tokens/{addr}/pools listing answer:
      is_base  — does GT hold pool_address with the token in the BASE seat?
                 (None when the pool isn't in the listing at all)
      candidates — other pools where the token IS GT's base, sorted by
                 reserve desc, only reserve >= min_reserve.
    One listing call answers both — no extra request."""
    rows = ((listing or {}).get("data") or [])
    want_token = token_address.lower()
    want_pool = pool_address.lower()
    is_base: Optional[bool] = None
    cands: List[dict] = []
    for p in rows:
        attrs = p.get("attributes") or {}
        rel = p.get("relationships") or {}
        base_id = _bare(((rel.get("base_token") or {}).get("data") or {}).get("id") or "")
        pid = (attrs.get("address") or _bare(p.get("id") or ""))
        token_is_base = base_id.lower() == want_token
        if pid.lower() == want_pool:
            is_base = token_is_base
        if token_is_base and pid.lower() != want_pool:
            try:
                reserve = float(attrs.get("reserve_in_usd") or 0)
            except (TypeError, ValueError):
                reserve = 0.0
            cands.append({"pool_address": pid, "reserve_usd": reserve,
                          "name": attrs.get("name") or ""})
    cands = [c for c in cands if c["reserve_usd"] >= 1000]
    cands.sort(key=lambda c: c["reserve_usd"], reverse=True)
    return is_base, cands


def confirm_seats(client_v2, token_address: str, pool_address: str):
    """The single GT listing call behind the tripwires. Returns
    (is_base, candidates); on any API failure (None, []) so callers fail
    open to the OLD behavior rather than blocking the pipeline."""
    try:
        data = client_v2._request(
            f"/networks/{client_v2.network}/tokens/{token_address}/pools",
            params={"page": 1})
    except Exception:  # noqa: BLE001 — third-party must never break pricing
        return None, []
    try:
        return parse_pools(data, token_address, pool_address)
    except Exception:  # noqa: BLE001
        return None, []
