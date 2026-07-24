"""
Solana address parser.

Extracts Solana mint addresses from Telegram message text. Per the agreed
channel-only model: a message with multiple addresses yields ONE call using
the first match; messages with no match are dropped.

Also attempts to extract a token symbol/ticker from surrounding context
(e.g. "$BONK" or "BONK/SOL") as a convenience, but the canonical symbol/name
always comes from GeckoTerminal (token_meta).
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from models import ParsedCall, RawMessage

# Base58 (no 0/O/I/l) Solana mint addresses are 32-44 chars.
SOLANA_ADDR_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")

# Common ticker patterns: $SYM, SYM/SOL, SYM-USDC. $-prefix is the dominant one.
TICKER_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,9})")

# ---- Bot / trade-notification patterns to exclude --------------------------
# These match the AgentBIBI-style automated "BUY!" posts, solscan buyer links,
# and similar non-human-call messages. A message matching ANY of these is
# treated as bot spam and filtered out.
_BOT_PATTERN_RE = re.compile(
    r"""
    (?:🟡{3,})                           # 🟡🟡🟡 blocks (trade-notification decoration)
    |(?:➡️\s*SOL[:\s])                   # "➡️ SOL: 77.45" style trade summary
    |(?:solscan\.io/[a-z]/)              # solscan buyer/txn links in body
    |(?:⚡\s*\d[\d,]*\s*(?:SOL|sol))     # "⚡ 26.68 SOL" lightning-amount pattern
    |(?:\b(?:BUY|SELL|BOUGHT|SOLD)\s*[!！])  # "BUY!", "SELL！" all-caps bot alerts
    |(?:⬅️\s*\d)                         # "⬅️ 1.8M" token-amount pattern
    |(?:\bWallet\s*[:：]\s*`?           # "Wallet:" followed by an address — this is
                                          # NOT a call, it's the caller sharing their
                                          # own wallet address for followers to track.
                                          # The address after it is a WALLET, not a token.
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Dedup is now WINDOW-WIDE (not 24h): once a token/ticker is called, every
# subsequent mention is treated as a pump-update and dropped. Only the FIRST
# call ever counts. This prevents the same token from inflating the call count
# and skewing the win rate.


def is_bot_spam(text: str) -> bool:
    """Return True if the message matches known bot / trade-notification patterns."""
    if not text:
        return False
    return bool(_BOT_PATTERN_RE.search(text))


def is_valid_solana_address(addr: str) -> bool:
    """
    Basic validation that an address looks like a Solana mint/pool (base58).
    Rejects: hex strings, 0x/EVM-prefixed (incl. stripped 0x like 'x12ab...'),
    too-short/long.
    """
    if not addr or len(addr) < 32 or len(addr) > 44:
        return False
    # Hex strings (case-insensitive) are never Solana base58.
    if re.fullmatch(r'[0-9a-fA-F]{32,44}', addr):
        return False
    # Leading 'x' followed by hex is likely a stripped 0x EVM address (mixed case).
    if addr.startswith('x') and re.fullmatch(r'x[0-9a-fA-F]{31,43}', addr):
        return False
    # Must match the base58 character set (no 0, O, I, l).
    return bool(SOLANA_ADDR_RE.fullmatch(addr))


def extract_addresses(text: str) -> List[str]:
    """All Solana-like addresses found in text, in order of appearance.

    Filters out addresses that are clearly not Solana (hex strings,
    stripped EVM 0x addresses, wrong length).
    """
    if not text:
        return []
    raw = SOLANA_ADDR_RE.findall(text)
    return [a for a in raw if is_valid_solana_address(a)]


def extract_ticker(text: str) -> Optional[str]:
    """Best-effort ticker extraction (without the $). None if not found."""
    if not text:
        return None
    m = TICKER_RE.search(text)
    return m.group(1).upper() if m else None


def parse_message(msg: RawMessage) -> Optional[ParsedCall]:
    """
    Convert a raw message into at most one ParsedCall.
    Returns None if the message contains no Solana address or matches bot spam.
    """
    if is_bot_spam(msg.text):
        return None
    addresses = extract_addresses(msg.text)
    if not addresses:
        return None

    # First match wins (channel-only simplification).
    token_address = addresses[0]
    return ParsedCall(
        channel_id=msg.channel_id,
        message_id=msg.message_id,
        raw_text=msg.text,
        token_address=token_address,
        token_symbol=extract_ticker(msg.text),
        timestamp=msg.timestamp,
    )


def deduplicate_calls(calls: List[ParsedCall]) -> List[ParsedCall]:
    """
    Deduplicate calls across the ENTIRE window: each unique token (by address
    OR ticker) appears AT MOST ONCE. Only the FIRST mention counts — all
    subsequent mentions are pump-updates and are dropped.

    This matches the caller model: a caller mentions a token once (the "call"),
    then posts updates/pumps about it later. The entry price must be from the
    FIRST mention, not a later update after the token has already pumped.

    Dedup keys on BOTH the raw address (case-insensitive) AND the ticker — if
    either matches an existing call, it's a duplicate.
    """
    seen: set = set()  # keys we've already kept
    addr_to_key: Dict[str, tuple] = {}  # raw/lower addr -> key
    sym_to_key: Dict[str, tuple] = {}  # ticker -> key
    out: List[ParsedCall] = []
    for call in calls:
        raw_addr = call.token_address
        raw_lower = raw_addr.lower()
        sym = (call.token_symbol or "").upper()

        # Check if we've seen this address (exact or case-insensitive)
        # or the same ticker.
        key = addr_to_key.get(raw_addr) or addr_to_key.get(raw_lower)
        if key is None and sym:
            key = sym_to_key.get(sym)

        if key is None:
            key = (call.channel_id, raw_lower)
            addr_to_key[raw_addr] = key
            addr_to_key[raw_lower] = key
            if sym:
                sym_to_key[sym] = key

        if key not in seen:
            seen.add(key)
            out.append(call)
        # else: token already called before — skip (pump-update)
    return out
