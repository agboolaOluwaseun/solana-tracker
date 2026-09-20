"""
Multi-chain detection and resolution.

Detects which blockchain a token address belongs to using:
1. Primary: URL context and explicit tagging in the message
2. Fallback: DexScreener API query (highest liquidity pool wins)
"""
import re
import logging
from typing import Optional

log = logging.getLogger(__name__)

# Internal chain name -> DexScreener chain ID mapping
DS_CHAIN_MAP = {
    "sol": "solana",
    "robinhood": "robinhood",
    "eth": "ethereum",
    "bsc": "bsc",
    "base": "base",
    "arc": "arc"
}

# URL patterns for chain detection
CHAIN_URL_PATTERNS = {
    "eth": [
        r"dexscreener\.com/ethereum/",
        r"etherscan\.io",
        r"uniswap\.org",
    ],
    "bsc": [
        r"dexscreener\.com/bsc/",
        r"bscscan\.com",
        r"pancakeswap\.finance",
    ],
    "base": [
        r"dexscreener\.com/base/",
        r"basescan\.org",
        r"base\.org",
    ],
    "arc": [
        r"dexscreener\.com/arc/",
        r"arcscan\.app",
    ],
    "robinhood": [
        r"dexscreener\.com/robinhood/",
        r"robinhood\.com",
    ]
}

# Explicit tag patterns
CHAIN_TAG_PATTERNS = {
    "eth": [
        r"\b(ETH|Ethereum|Ethereum\s*mainnet)\s*[:\-]?\s*",
        r"\b(ERC20|ERC-20)\b",
    ],
    "bsc": [
        r"\b(BSC|BNB\s*Chain|Binance\s*Smart\s*Chain)\s*[:\-]?\s*",
        r"\b(BEP20|BEP-20)\b",
    ],
    "base": [
        r"\b(Base|Base\s*Chain)\s*[:\-]?\s*",
    ],
    "arc": [
        r"\b(Arc|Arc\s*Chain|Circle\s*Arc)\s*[:\-]?\s*",
    ],
    "robinhood": [
        r"\b(Robinhood|RH)\s*[:\-]?\s*",
    ]
}


def detect_chain_from_context(text: str, address: str) -> Optional[str]:
    """
    Detect chain from URL context or explicit tags in the message.
    
    Args:
        text: The message text containing the address
        address: The token address to detect chain for
    
    Returns:
        Chain name ("eth", "bsc", "base", "arc", "robinhood") or None if not detected
    """
    if not text:
        return None
    text_lower = text.lower()
    
    # 1. Check URL patterns
    for chain, patterns in CHAIN_URL_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, text_lower):
                log.debug(f"Chain detected via URL pattern: {chain} for {address[:10]}...")
                return chain
    
    # 2. Check explicit tags
    for chain, patterns in CHAIN_TAG_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                log.debug(f"Chain detected via explicit tag: {chain} for {address[:10]}...")
                return chain
    
    return None


def detect_chain_from_dexscreener(address: str) -> Optional[str]:
    """
    Fallback: Query DexScreener API to detect chain by finding the highest
    liquidity pool for this address.
    
    Args:
        address: The token address to detect chain for
    
    Returns:
        Chain name or None if not found
    """
    try:
        # SHARED client (module-level singleton in pipeline._get_ds_client):
        # one instance = one pacing clock. Spawning a fresh DexScreenerClient
        # per address resets _last_call and bypasses our ~2/s discipline.
        from pipeline import _get_ds_client
        client = _get_ds_client()
        
        # Query DexScreener for this token
        pairs = client.get_pairs_by_token(address)
        
        if not pairs:
            log.debug(f"No DexScreener pairs found for {address[:10]}...")
            return None
        
        # Find the pair with highest liquidity
        best_pair = None
        best_liquidity = -1
        
        for pair in pairs:
            liquidity = pair.get("liquidity", {}).get("usd", 0) or 0
            if liquidity > best_liquidity:
                best_liquidity = liquidity
                best_pair = pair
        
        if not best_pair:
            return None
        
        # Map DexScreener chain ID back to our internal name
        ds_chain_id = best_pair.get("chainId", "").lower()
        
        # Reverse mapping: DexScreener chain ID -> internal chain name
        reverse_map = {v: k for k, v in DS_CHAIN_MAP.items()}
        chain = reverse_map.get(ds_chain_id)
        
        if chain:
            log.debug(f"Chain detected via DexScreener: {chain} for {address[:10]}... (liquidity: ${best_liquidity:,.0f})")
            return chain
        
        log.warning(f"Unknown DexScreener chain ID: {ds_chain_id} for {address[:10]}...")
        return None
        
    except Exception as e:
        log.warning(f"DexScreener chain detection failed for {address[:10]}...: {e}")
        return None


def detect_chain(text: str, address: str) -> str:
    """
    Detect which blockchain a token address belongs to.
    
    Strategy:
    1. Primary: URL context and explicit tags (fast, deterministic)
    2. Fallback: DexScreener API query (slower, but accurate)
    3. Default: "eth" for EVM addresses (most common case)
    
    Args:
        text: The message text containing the address
        address: The token address to detect chain for
    
    Returns:
        Chain name ("sol", "eth", "bsc", "base", "arc", "robinhood")
    """
    # Solana addresses are base58 (not 0x-prefixed)
    if not address.startswith("0x"):
        return "sol"
    
    # 1. Try context-based detection (URL / explicit tag)
    chain = detect_chain_from_context(text, address)
    if chain:
        return chain
    
    # 2. Try DexScreener API fallback (pair found = authoritative chain)
    chain = detect_chain_from_dexscreener(address)
    if chain:
        return chain
    
    # 3. No context AND no pair found anywhere: the token is unpriceable on
    #    every chain, so this bucket only affects display/dedup — keep the
    #    legacy default ('robinhood': channels posting bare 0x addresses in
    #    this deployment, e.g. 666, are Robinhood callers). Defaulting to
    #    'eth' here re-bucketed every unfindable RH call into Ethereum.
    log.info(f"chain detection fallback to legacy 'robinhood' for {address[:10]}...")
    return "robinhood"


def get_ds_chain_id(internal_chain: str) -> str:
    """
    Convert internal chain name to DexScreener chain ID.
    
    Args:
        internal_chain: Internal chain name ("sol", "eth", "bsc", etc.)
    
    Returns:
        DexScreener chain ID ("solana", "ethereum", "bsc", etc.)
    """
    return DS_CHAIN_MAP.get(internal_chain, internal_chain)
