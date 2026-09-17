"""
Tests for multi-chain detection logic.

Tests the three-tier chain detection:
1. URL context (dexscreener.com/ethereum/, etc.)
2. Explicit tags (ETH:, BSC:, etc.)
3. DexScreener API fallback (highest liquidity)
"""
import pytest
from unittest.mock import patch, MagicMock
from chains.chain_detector import detect_chain, detect_chain_from_context


class TestURLContextDetection:
    """Test chain detection from URL patterns."""

    def test_ethereum_url(self):
        text = "Check out this token on dexscreener.com/ethereum/0x1234567890abcdef1234567890abcdef12345678"
        assert detect_chain_from_context(text, "0x1234567890abcdef1234567890abcdef12345678") == "eth"

    def test_bsc_url(self):
        text = "New token: dexscreener.com/bsc/0xabcdef1234567890abcdef1234567890abcdef12"
        assert detect_chain_from_context(text, "0xabcdef1234567890abcdef1234567890abcdef12") == "bsc"

    def test_base_url(self):
        text = "Base chain token: dexscreener.com/base/0x1234567890abcdef1234567890abcdef12345678"
        assert detect_chain_from_context(text, "0x1234567890abcdef1234567890abcdef12345678") == "base"

    def test_arc_url(self):
        text = "Arc network: dexscreener.com/arc/0xabcdef1234567890abcdef1234567890abcdef12"
        assert detect_chain_from_context(text, "0xabcdef1234567890abcdef1234567890abcdef12") == "arc"

    def test_robinhood_url(self):
        text = "Robinhood token: dexscreener.com/robinhood/0x1234567890abcdef1234567890abcdef12345678"
        assert detect_chain_from_context(text, "0x1234567890abcdef1234567890abcdef12345678") == "robinhood"

    def test_etherscan_url(self):
        text = "Contract on etherscan.io/address/0x1234567890abcdef1234567890abcdef12345678"
        assert detect_chain_from_context(text, "0x1234567890abcdef1234567890abcdef12345678") == "eth"

    def test_bscscan_url(self):
        text = "BSC contract: bscscan.com/token/0xabcdef1234567890abcdef1234567890abcdef12"
        assert detect_chain_from_context(text, "0xabcdef1234567890abcdef1234567890abcdef12") == "bsc"

    def test_basescan_url(self):
        text = "Base contract: basescan.org/address/0x1234567890abcdef1234567890abcdef12345678"
        assert detect_chain_from_context(text, "0x1234567890abcdef1234567890abcdef12345678") == "base"

    def test_arcscan_url(self):
        text = "Arc contract: arcscan.app/address/0xabcdef1234567890abcdef1234567890abcdef12"
        assert detect_chain_from_context(text, "0xabcdef1234567890abcdef1234567890abcdef12") == "arc"

    def test_no_url_context(self):
        text = "Just a random message with no chain context"
        assert detect_chain_from_context(text, "0x1234567890abcdef1234567890abcdef12345678") is None


class TestExplicitTagDetection:
    """Test chain detection from explicit tags."""

    def test_eth_tag(self):
        text = "ETH: 0x1234567890abcdef1234567890abcdef12345678"
        assert detect_chain_from_context(text, "0x1234567890abcdef1234567890abcdef12345678") == "eth"

    def test_ethereum_tag(self):
        text = "Ethereum: 0x1234567890abcdef1234567890abcdef12345678"
        assert detect_chain_from_context(text, "0x1234567890abcdef1234567890abcdef12345678") == "eth"

    def test_bsc_tag(self):
        text = "BSC: 0xabcdef1234567890abcdef1234567890abcdef12"
        assert detect_chain_from_context(text, "0xabcdef1234567890abcdef1234567890abcdef12") == "bsc"

    def test_bnb_chain_tag(self):
        text = "BNB Chain: 0xabcdef1234567890abcdef1234567890abcdef12"
        assert detect_chain_from_context(text, "0xabcdef1234567890abcdef1234567890abcdef12") == "bsc"

    def test_base_tag(self):
        text = "Base: 0x1234567890abcdef1234567890abcdef12345678"
        assert detect_chain_from_context(text, "0x1234567890abcdef1234567890abcdef12345678") == "base"

    def test_arc_tag(self):
        text = "Arc: 0xabcdef1234567890abcdef1234567890abcdef12"
        assert detect_chain_from_context(text, "0xabcdef1234567890abcdef1234567890abcdef12") == "arc"

    def test_robinhood_tag(self):
        text = "Robinhood: 0x1234567890abcdef1234567890abcdef12345678"
        assert detect_chain_from_context(text, "0x1234567890abcdef1234567890abcdef12345678") == "robinhood"

    def test_erc20_tag(self):
        text = "ERC20 token: 0x1234567890abcdef1234567890abcdef12345678"
        assert detect_chain_from_context(text, "0x1234567890abcdef1234567890abcdef12345678") == "eth"

    def test_bep20_tag(self):
        text = "BEP20 token: 0xabcdef1234567890abcdef1234567890abcdef12"
        assert detect_chain_from_context(text, "0xabcdef1234567890abcdef1234567890abcdef12") == "bsc"

    def test_case_insensitive(self):
        text = "eth: 0x1234567890abcdef1234567890abcdef12345678"
        assert detect_chain_from_context(text, "0x1234567890abcdef1234567890abcdef12345678") == "eth"

    def test_no_tag(self):
        text = "Just a random message with no chain tag"
        assert detect_chain_from_context(text, "0x1234567890abcdef1234567890abcdef12345678") is None


class TestSolanaDetection:
    """Test Solana address detection (non-EVM)."""

    def test_solana_address(self):
        text = "Solana token: So11111111111111111111111111111111111111112"
        # Solana addresses are base58, not 0x-prefixed
        assert detect_chain(text, "So11111111111111111111111111111111111111112") == "sol"

    def test_solana_address_with_eth_context(self):
        # Even if there's ETH context, Solana addresses should be detected as Solana
        text = "ETH: So11111111111111111111111111111111111111112"
        assert detect_chain(text, "So11111111111111111111111111111111111111112") == "sol"


class TestDexScreenerFallback:
    """Test DexScreener API fallback for chain detection."""

    @patch("pipeline._get_ds_client")
    def test_fallback_highest_liquidity(self, mock_get_client):
        """Should pick the chain with highest liquidity."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        
        # Mock response with multiple chains
        mock_client.get_pairs_by_token.return_value = [
            {"chainId": "ethereum", "liquidity": {"usd": 100000}},
            {"chainId": "bsc", "liquidity": {"usd": 500000}},  # Highest
            {"chainId": "base", "liquidity": {"usd": 200000}},
        ]
        
        from chains.chain_detector import detect_chain_from_dexscreener
        result = detect_chain_from_dexscreener("0x1234567890abcdef1234567890abcdef12345678")
        assert result == "bsc"

    @patch("pipeline._get_ds_client")
    def test_fallback_no_pairs(self, mock_get_client):
        """Should return None when no pairs found."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.get_pairs_by_token.return_value = []
        
        from chains.chain_detector import detect_chain_from_dexscreener
        result = detect_chain_from_dexscreener("0x1234567890abcdef1234567890abcdef12345678")
        assert result is None

    @patch("pipeline._get_ds_client")
    def test_fallback_unknown_chain(self, mock_get_client):
        """Should return None for unknown chain IDs."""
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        mock_client.get_pairs_by_token.return_value = [
            {"chainId": "unknown_chain", "liquidity": {"usd": 100000}},
        ]
        
        from chains.chain_detector import detect_chain_from_dexscreener
        result = detect_chain_from_dexscreener("0x1234567890abcdef1234567890abcdef12345678")
        assert result is None


class TestDetectChainIntegration:
    """Integration tests for the full detect_chain function."""

    def test_priority_url_over_tag(self):
        """URL context should take priority over explicit tags."""
        text = "ETH: 0x1234567890abcdef1234567890abcdef12345678 on dexscreener.com/bsc/0x1234567890abcdef1234567890abcdef12345678"
        # URL says BSC, tag says ETH - URL should win
        assert detect_chain(text, "0x1234567890abcdef1234567890abcdef12345678") == "bsc"

    @patch("chains.chain_detector.detect_chain_from_dexscreener")
    def test_fallback_when_no_context(self, mock_fallback):
        """Should use DexScreener fallback when no context available."""
        text = "Random message with no chain context"
        mock_fallback.return_value = "eth"
        
        result = detect_chain(text, "0x1234567890abcdef1234567890abcdef12345678")
        assert result == "eth"
        mock_fallback.assert_called_once_with("0x1234567890abcdef1234567890abcdef12345678")

    @patch("chains.chain_detector.detect_chain_from_dexscreener")
    def test_default_to_eth_when_all_fail(self, mock_fallback):
        """Should default to Ethereum when all detection methods fail."""
        text = "Random message"
        mock_fallback.return_value = None
        
        result = detect_chain(text, "0x1234567890abcdef1234567890abcdef12345678")
        assert result == "eth"


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_empty_text(self):
        """Should handle empty text gracefully."""
        assert detect_chain_from_context("", "0x1234567890abcdef1234567890abcdef12345678") is None

    def test_none_text(self):
        """Should handle None text gracefully."""
        assert detect_chain_from_context(None, "0x1234567890abcdef1234567890abcdef12345678") is None

    def test_multiple_urls_different_chains(self):
        """Should pick the first matching URL."""
        text = "Check dexscreener.com/ethereum/0x1234 and dexscreener.com/bsc/0x5678"
        # First URL match wins
        assert detect_chain_from_context(text, "0x1234567890abcdef1234567890abcdef12345678") == "eth"

    def test_url_without_chain_prefix(self):
        """Should not match URLs without chain prefix."""
        text = "Check dexscreener.com/0x1234567890abcdef1234567890abcdef12345678"
        assert detect_chain_from_context(text, "0x1234567890abcdef1234567890abcdef12345678") is None
