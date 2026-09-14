"""
Robinhood token-call parser — fresh regex for EVM-style addresses.

Robinhood Chain is an EVM L1: token addresses are 0x + 40 hex chars
(standard ERC-20 contract addresses).  This is fundamentally different from
Solana's 44-char base58; a bare `0x[hex]{40}` with hex-boundaries avoids
both false matches inside longer hex ids (Uniswap v4 pool keys are 64 hex,
stock-token uids are 66 hex) and Solana addresses entirely.

Call format observed in 666 🔥 Calls:
    "brainrot paired w aapl stock 87k 0x380b789970dccb4a0d81c818915db25a19b0fc80"
    "$turret  0x99d70a25bd7e95a30e14bcbb64752c92227de9d7"
Bot-mirror messages repeat the same address -> dedup happens downstream
(keyed on token address), never here.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

# Exactly 40 hex chars, bounded by non-hex on both sides (case-insensitive).
EVM_ADDRESS_RE = re.compile(r"(?<![0-9a-fA-F])0x[0-9a-fA-F]{40}(?![0-9a-fA-F])")
# DexScreener/GNOSIS-style pool keys are 0x + 64 hex (composite pair id).
# These appear in dexscreener /robinhood/ URLs and resolve to their pair via
# DexScreener search — mined ONLY from allowlisted URLs, never bare in text
# (a raw 64-hex in prose is usually a tx hash, not a call).
EVM_POOL_RE = re.compile(r"(?<![0-9a-fA-F])0x[0-9a-fA-F]{64}(?![0-9a-fA-F])")
# Symbol hints: $TICKER or a bare word adjacent to the address.
SYMBOL_RE = re.compile(r"\$([A-Za-z0-9_]{1,20})")
# Market cap hints like "87k", "400k", "1.2m" (loose; used as context only).
# URLs: markdown links and bare http(s) links.  Addresses inside MOST URLs are
# NEVER calls — the channel's bot-mirror messages ("Achievement Unlocked",
# "made a x2+ call on [Token](t.me/spydefi_bot?start=0x...)") embed the
# token address as a URL parameter, and those must not be parsed as calls.
# EXCEPTION (Civilian Degens pattern): channels that post calls as DEX links.
# Addresses inside a curated allowlist of DEX/explorer hosts ARE calls:
# dexscreener.com/robinhood/<pool>, gate.com/alpha/robinhood-<token>,
# geckoterminal/dextools/gmgn pool URLs, explorers, etc. A pool-vs-token
# ambiguity is fine: the pricing resolver's DexScreener search fallback maps
# a pool address to its pair, and post-pricing identity dedup merges dupes.
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")
_BARE_URL_RE = re.compile(r"https?://\S+")

# Allowlisted hosts whose embedded 0x addresses count as calls — chain-scoped
# paths only, so dexscreener.com/bsc/0x… (a BSC token) never enters as a
# Robinhood call. Robinhood-chain links: dexscreener/robinhood, geckoterminal
# /networks/robinhood, gate.com/alpha/robinhood-0x…, the Robinhood explorer.
_DEX_URL_RE = re.compile(
    r"""https?://[^\s"']*(?:
          dexscreener\.(?:com|io)/robinhood
        | geckoterminal\.com/dex-pools/networks/robinhood
        | geckoterminal\.com/networks/robinhood
        | gate\.com/alpha/robinhood-
        | explorer\.robinhood\.com/(?:address|token)
        | dextools\.io/app/pair/chains/robinhood
    )[^\s"']*""",
    re.IGNORECASE | re.VERBOSE,
)


def _addresses_in_allowed_urls(text: str) -> "list[tuple[str, int]]":
    """(addr, position) for 0x40hex tokens AND 0x64hex pool keys inside
    allowlisted DEX URLs only."""
    out = []
    for um in _DEX_URL_RE.finditer(text):
        url = um.group(0)
        for rx in (EVM_ADDRESS_RE, EVM_POOL_RE):
            for am in rx.finditer(url):
                out.append((am.group(0).lower(), um.start() + am.start()))
    return out


def _strip_urls(text: str) -> str:
    """Remove URL anchors (keeping link text) and bare URLs."""
    text = _MD_LINK_RE.sub(lambda m: m.group(1) or "", text)
    text = _BARE_URL_RE.sub("", text)
    return text


@dataclass
class ParsedCall:
    channel_id: int
    message_id: int
    token_address: str  # lowercase 0x + 40 hex
    token_symbol: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)
    raw_text: str = ""


def _symbol_hint(text: str, addr_start: int) -> Optional[str]:
    """Best-effort symbol: $TICKER anywhere in the message, else None."""
    before = text[:addr_start]
    m = list(SYMBOL_RE.finditer(before))
    if m:
        return m[-1].group(1)
    return None


def parse_calls(
    channel_id: int,
    message_id: int,
    text: str,
    timestamp: datetime,
) -> List[ParsedCall]:
    """Extract all distinct EVM token addresses from one message.

    Returns one ParsedCall per unique address (dedup within the message —
    mirrors/links can repeat the same address multiple times).
    """
    seen = set()
    out: List[ParsedCall] = []
    # 1) Allowlisted DEX/explorer URLs carry the actual call (pool or token).
    for addr, pos in _addresses_in_allowed_urls(text):
        if addr in seen:
            continue
        seen.add(addr)
        out.append(
            ParsedCall(
                channel_id=channel_id,
                message_id=message_id,
                token_address=addr,
                token_symbol=_symbol_hint(text, pos),
                timestamp=timestamp,
                raw_text=text,
            )
        )
    # 2) Regular in-text addresses (URLs stripped so bot-mirror links and
    #    non-allowlisted hosts can never inject calls).
    stripped = _strip_urls(text)
    for m in EVM_ADDRESS_RE.finditer(stripped):
        addr = m.group(0).lower()
        if addr in seen:
            continue
        seen.add(addr)
        out.append(
            ParsedCall(
                channel_id=channel_id,
                message_id=message_id,
                token_address=addr,
                token_symbol=_symbol_hint(stripped, m.start()),
                timestamp=timestamp,
                raw_text=text,
            )
        )
    return out