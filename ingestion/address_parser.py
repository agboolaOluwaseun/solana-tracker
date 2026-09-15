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

# URLs — markdown links and bare http(s) links. Addresses inside ALL non-
# allowlisted URLs (bot deep-links like t.me/RickBurpBot?start=<mint>, x.com,
# explorers) must never be treated as calls (mirror of the Robinhood parser).
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
_BARE_URL_RE = re.compile(r"https?://\S+")

# Common ticker patterns: $SYM, SYM/SOL, SYM-USDC. $-prefix is the dominant one.
TICKER_RE = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,9})")

# ---- Bot / trade-notification patterns to exclude --------------------------
# These match the AgentBIBI-style automated "BUY!" posts, solscan buyer links,
# and similar non-human-call messages. A message matching ANY of these is
# treated as bot spam and filtered out.
#
# NOTE: The BUY/SELL/BOUGHT/SOLD alert pattern is case-sensitive (all-caps only).
# Bot trade alerts always use ALL CAPS. Genuine calls use lowercase "buy" in
# sentences like "best time to buy !". A single IGNORECASE regex would false-
# positive on those, so the text-word pattern is split into its own regex.
_BOT_PATTERN_RE = re.compile(
    r"""
    (?:🟡{3,})                           # 🟡🟡🟡 blocks (trade-notification decoration)
    |(?:➡️\s*SOL[:\s])                   # "➡️ SOL: 77.45" style trade summary
    |(?:solscan\.io/[a-z]/)              # solscan buyer/txn links in body
    |(?:⚡\s*\d[\d,]*\s*(?:SOL|sol))     # "⚡ 26.68 SOL" lightning-amount pattern
    |(?:⬅️\s*\d)                         # "⬅️ 1.8M" token-amount pattern
    |(?:\bWallet\s*[:：]\s*`?           # "Wallet:" followed by an address — this is
                                          # NOT a call, it's the caller sharing their
                                          # own wallet address for followers to track.
                                          # The address after it is a WALLET, not a token.
    |(?:@\w+[Bb]uy[Bb]ot)               # @MajorBuyBot, @BuyBot, etc. — buybot mentions
    |(?:majorbots\.io/buybot/)           # MajorBuyBot chart links
    |(?:Position:\s*New\s*Holder)        # buybot "Position: New Holder" line
    |(?:Wallet\s*Balance:\s*\$)          # buybot "Wallet Balance: $X" line
    |(?:MCap:\s*\$)                      # buybot "MCap: $X" line
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Case-sensitive: bot alerts are ALWAYS all-caps. Using IGNORECASE here would
# match "buy !" in genuine call text like "best time to buy !".
_BOT_ALERT_RE = re.compile(
    r"""\b(?:BUY|SELL|BOUGHT|SOLD)\s*[!！]""",
    re.VERBOSE,
)

# solscan URLs whose path is a TRANSACTION or ACCOUNT (wallet) address, not a
# token mint. The base58 string inside these links must never be treated as a
# call — it burns resolve API calls and lands as unpriceable noise. We strip
# the whole URL before extraction so a real token address elsewhere in the
# same message still extracts.
_SOLSCAN_NON_TOKEN_URL_RE = re.compile(
    r"""https?://solscan\.io/(?:tx|account|address)/[1-9A-HJ-NP-Za-km-z]+""",
    re.IGNORECASE,
)

# DexScreener URLs: the path is a POOL address, not a token mint. We strip
# these before extraction so the pool address is never treated as a call.
_DEXSCREENER_URL_RE = re.compile(
    r"""https?://(?:www\.)?dexscreener\.com/solana/[^\s]+""",
    re.IGNORECASE,
)

# EXCEPTION to URL-stripping (Civilian Degens pattern): channels that post
# calls AS DEX links. An address inside a curated allowlist of DEX/explorer
# hosts IS the call. On dexscreener/geckoterminal/dextools/gmgn the path is
# the POOL; pricing resolves pool addresses through the DexScreener search
# fallback in price_one_call (pool -> its base token pair), and post-pricing
# identity dedup merges the pool-URL row with any later mint mention.
_DEX_URL_RE = re.compile(
    r"""https?://[^\s"']*(?:
          dexscreener\.(?:com|io)/solana
        | geckoterminal\.com/solana/pools
        | dextools\.io/app/pair/chains/solana
        | gmgn\.ai/sol/pool
        | birdeye\.so/token
        | pump\.fun/
        | jup\.ag/
        | solscan\.io/token/
        | raydium\.io/
        | coinmarketcap\.com/cryptocurrencies/
    )[^\s"']*""",
    re.IGNORECASE | re.VERBOSE,
)


def _addresses_in_allowed_dex_urls(text: str) -> "list[str]":
    """Base58 addresses embedded in allowlisted DEX/explorer URLs."""
    out: List[str] = []
    for um in _DEX_URL_RE.finditer(text):
        url = um.group(0)
        # Drop non-token path segments (solscan tx/account already stripped
        # upstream; guard again here) and query params like ?chain=solana.
        for tok in re.split(r"[/?&=:,]", url):
            if SOLANA_ADDR_RE.fullmatch(tok) and is_valid_solana_address(tok):
                out.append(tok)
    return out

# Presale detection: messages announcing presales (pinksale, "presale starts",
# "presale begins", etc.) should not be counted as calls — the token isn't
# trading yet, so there's no entry price to score.
_PRESALE_RE = re.compile(
    r"""
    (?:pinksale\.finance)              # PinkSale launchpad URLs
    |(?:\bpresale\s+(?:starts|begins|launches|opens))  # "presale starts", "presale begins"
    |(?:\bpresale\s+today)             # "presale today"
    |(?:\bpresale\s+available)         # "presale available"
    |(?:\bclaim\s+is\s+now\s+available)  # "claim is now available" (post-presale)
    |(?:\bpre[\s-]?sale\s+\d)          # "presale 1", "pre-sale 2"
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
    if _BOT_PATTERN_RE.search(text):
        return True
    if _BOT_ALERT_RE.search(text):
        return True
    return False


def is_valid_solana_address(addr: str) -> bool:
    """
    Basic validation that an address looks like a Solana mint/pool (base58).
    Rejects: hex strings, 0x/EVM-prefixed (incl. stripped 0x like 'x12ab...'),
    too-short/long, and obviously fake addresses (e.g. repeated characters).
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
    if not SOLANA_ADDR_RE.fullmatch(addr):
        return False
    # Filter out obviously fake addresses:
    # 1. Extremely low entropy — real addresses have at least 3 unique chars.
    #    "Hahaha..." has only 2 (h, a). System addresses like wrapped SOL have 4+.
    unique_chars = len(set(addr.lower()))
    if unique_chars < 3:
        return False
    # 2. Repeating 2-char pattern (e.g. "HaHaHa...", "ababab...")
    #    Check if the address is mostly a 2-char repeating sequence
    if len(addr) >= 32 and unique_chars == 2:
        pattern = addr[:2].lower()
        if all(addr[i:i+2].lower() == pattern for i in range(0, len(addr)-1, 2)):
            return False
    return True


def extract_addresses(text: str) -> List[str]:
    """All Solana-like addresses found in text, in order of appearance.

    Filters out addresses that are clearly not Solana (hex strings,
    stripped EVM 0x addresses, wrong length). solscan tx/account URLs and
    DexScreener pool URLs are removed first so their addresses are never
    treated as calls. Also filters out addresses that appear right after
    wallet-related phrases (e.g. "my wallet:", "Wallet:").
    """
    if not text:
        return []
    # Calls posted AS DEX links (Civilian Degens): pull the address from the
    # URL first, before the general strip removes it. Chain-anchored pattern,
    # so /bsc/ /eth/ links on shared hosts can never inject Solana calls.
    url_addrs = _addresses_in_allowed_dex_urls(text)
    # Strip ALL remaining URLs (t.me/RickBurpBot?start=<mint> bot-digest
    # deep-links, solscan tx/account, x.com, and non-allowlist explorers) so
    # addresses embedded in links are never treated as calls — same rule the
    # Robinhood parser applies. solscan/dexscreener patterns are subsumed by
    # this; kept explicit for documentation + the allowlist mined above.
    text = _SOLSCAN_NON_TOKEN_URL_RE.sub(" ", text)
    text = _DEXSCREENER_URL_RE.sub(" ", text)
    text = _BARE_URL_RE.sub(" ", text)
    text = _MD_LINK_RE.sub(lambda m: m.group(1) or "", text)
    
    # Find all candidate addresses
    raw = SOLANA_ADDR_RE.findall(text)
    candidates = [a for a in raw if is_valid_solana_address(a)]
    
    # Filter out addresses that appear right after wallet-related phrases
    # (e.g. "my wallet: <addr>", "Wallet: <addr>", "tracking wallet <addr>")
    wallet_context_re = re.compile(
        r"""(?:my\s+wallet|wallet|tracking\s+wallet|trenching\s+wallet)\s*[:：]?\s*[`"]?([1-9A-HJ-NP-Za-km-z]{32,44})""",
        re.IGNORECASE,
    )
    wallet_addrs = {m.group(1) for m in wallet_context_re.finditer(text)}
    candidates = [a for a in candidates if a not in wallet_addrs]

    # In-text addresses win (a mint beats a pool link); URL addresses are the
    # fallback for link-only posts. Order preserved, deduped.
    merged: List[str] = []
    for a in candidates + url_addrs:
        if a not in merged:
            merged.append(a)
    return merged


def extract_ticker(text: str) -> Optional[str]:
    """Best-effort ticker extraction (without the $). None if not found."""
    if not text:
        return None
    m = TICKER_RE.search(text)
    return m.group(1).upper() if m else None


def parse_message(msg: RawMessage) -> Optional[ParsedCall]:
    """
    Convert a raw message into at most one ParsedCall.
    Returns None if the message contains no Solana address, matches bot spam,
    or is a presale announcement (token not yet trading — no entry price to score).
    """
    if is_bot_spam(msg.text):
        return None
    # Presale announcements: token isn't trading yet, so no entry price to score.
    # Exclude these entirely — they'd land as unpriceable noise.
    if _PRESALE_RE.search(msg.text):
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
    Deduplicate calls WITHIN EACH CHANNEL: same token within one channel
    appears AT MOST ONCE. Different channels can independently call the
    same token without interfering with each other.

    This matches the caller model: a caller mentions a token once (the "call"),
    then posts updates/pumps about it later. The entry price must be from the
    FIRST mention, not a later update after the token has already pumped.

    Dedup keys on BOTH the raw address (case-insensitive) AND the ticker — if
    either matches an existing call WITHIN THE SAME CHANNEL, it's a duplicate.

    Cross-channel: if Channel A and Channel B both call the same token, both
    calls are kept independently. Each channel has its own dedup scope.
    """
    seen: set = set()  # keys we've already kept
    # Per-channel dictionaries to prevent cross-channel contamination.
    # Keyed by channel_id → {address/ticker → dedup_key}.
    channel_addr_to_key: Dict[int, Dict[str, tuple]] = {}
    channel_sym_to_key: Dict[int, Dict[str, tuple]] = {}
    out: List[ParsedCall] = []
    # Sort by timestamp ascending so the OLDEST (original) call is kept,
    # not the newest (notification/update). iter_messages returns newest-first,
    # so without sorting we'd keep the update and drop the original.
    calls = sorted(calls, key=lambda c: c.timestamp)
    for call in calls:
        raw_addr = call.token_address
        raw_lower = raw_addr.lower()
        sym = (call.token_symbol or "").upper()
        ch = call.channel_id

        # Initialize per-channel dicts if first time seeing this channel
        if ch not in channel_addr_to_key:
            channel_addr_to_key[ch] = {}
            channel_sym_to_key[ch] = {}

        addr_to_key = channel_addr_to_key[ch]
        sym_to_key = channel_sym_to_key[ch]

        # Check if we've seen this address (exact or case-insensitive)
        # or the same ticker WITHIN THIS CHANNEL.
        key = addr_to_key.get(raw_addr) or addr_to_key.get(raw_lower)
        if key is None and sym:
            key = sym_to_key.get(sym)

        if key is None:
            key = (ch, raw_lower)
            addr_to_key[raw_addr] = key
            addr_to_key[raw_lower] = key
            if sym:
                sym_to_key[sym] = key

        if key not in seen:
            seen.add(key)
            out.append(call)
        # else: token already called in THIS channel — skip (pump-update)
    return out
