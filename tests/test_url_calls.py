"""Regression tests: calls posted AS DEX links (Civilian Degens pattern).

Both parsers used to strip ALL URLs before extraction, so channels whose
posts are comment + dexscreener/gate links yielded almost no calls. Fix:
addresses inside a CHAIN-SCOPED allowlist of DEX hosts are extracted; every
other URL (t.me bot-mirror params, cross-chain paths like /bsc//eth/) stays
dead — those regressions are tested explicitly here.
"""
from datetime import datetime

from models import RawMessage
from ingestion.address_parser import extract_addresses, parse_message
from chains.robinhood_impl import parser as rh

ADDR_POOL = ("0xa53e1fc004413094cb0a6a175b145ed085486a6ffa58bf61d55be48005f2cea2"
             )  # real KERMIT: 0x + 64-hex DexScreener pool key
ADDR_40 = "0xa53e1fc004413094cb0000000000000000000000"  # token-shaped 0x+40 hex
ADDR_TOKEN = "0xd111d37ba471fbe1c038976c8c51560bb0ee2335"  # token via gate.com
ADDR_BSC = "0xe9Bc5C6A86caA44fD7b469bf3cc7c563E4F77777"
SOL_MINT = "3TYgKwkE2Y3rxdw9osLRSpxpXmSC1C1oo19W9KHspump"
TME_TOKEN = "0x99d70a25bd7e95a30e14bcbb64752c92227de9d7"
NOW = datetime(2026, 9, 14, 12, 0, 0)


def rh_addrs(text):
    return [c.token_address for c in rh.parse_calls(1, 1, text, NOW)]


# ---- Robinhood chain: DEX links ARE calls ----------------------------------

def test_rh_dexscreener_robinhood_pool_key_url():
    text = "Someone dropped this in the lounge.\n\nhttps://dexscreener.com/robinhood/" + ADDR_POOL
    assert ADDR_POOL in rh_addrs(text)


def test_rh_gate_alpha_token_url():
    text = ("Gate Alpha just listed Kermit\n\n"
            "https://www.gate.com/alpha/robinhood-" + ADDR_TOKEN)
    assert ADDR_TOKEN in rh_addrs(text)


def test_rh_in_text_address_still_works():
    assert ADDR_40 in rh_addrs("$kermit look " + ADDR_40)


def test_rh_pool_key_only_from_urls_not_bare_prose():
    # 0x+64 pool keys are mined from allowlisted URLs only: in prose a 64-hex
    # string is usually a tx hash and must not count.
    assert ADDR_POOL in rh_addrs("chart https://dexscreener.com/robinhood/" + ADDR_POOL)
    assert ADDR_POOL not in rh_addrs("tx " + ADDR_POOL + " confirmed")


# ---- Robinhood chain: everything else stays dead ---------------------------

def test_rh_bsc_url_not_a_call():
    text = "Giggle academy. https://dexscreener.com/bsc/" + ADDR_BSC
    assert rh_addrs(text) == []


def test_rh_tme_bot_mirror_still_excluded():
    # The original protection: 666's bot-mirror posts embed the token as a
    # t.me deep-link param. Those are notifications, not calls.
    text = ("Huge win made by [666](https://t.me/spydefi_bot?start=" + TME_TOKEN + ")")
    assert rh_addrs(text) == []
    assert rh_addrs("see https://t.me/some_bot?start=" + TME_TOKEN) == []


def test_rh_random_explorer_url_not_a_call():
    assert rh_addrs("tx here https://etherscan.io/tx/" + ADDR_BSC) == []


# ---- Solana chain: dexscreener/solana links are calls -----------------------

def test_sol_dexscreener_solana_url():
    text = "2x and flying.\n\nhttps://dexscreener.com/solana/" + SOL_MINT
    assert SOL_MINT in extract_addresses(text)


def test_sol_pumpfun_link():
    assert SOL_MINT in extract_addresses("chart: https://pump.fun/coin/" + SOL_MINT)


def test_sol_bsc_link_not_solana():
    assert extract_addresses(
        "https://dexscreener.com/bsc/abcDEF123abcDEF123abcDEF123abcDEF123abcd") == []


def test_sol_solscan_tx_still_excluded():
    addr = "5xByHAGCQSPWrw6jPq3z5W9nV6t3rGzXQ8vDn2qKpumpXYZ"
    assert extract_addresses(f"https://solscan.io/tx/{addr}") == []
    assert extract_addresses(f"https://solscan.io/account/{addr}") == []


def test_sol_in_text_wins_over_url():
    other = "4Cn97ZE9QDFX7A8C78K6mjk91cB5Jd4dGkE3EjY3pump"
    text = other + " is live, also see https://dexscreener.com/solana/" + SOL_MINT
    addrs = extract_addresses(text)
    assert addrs[0] == other          # in-text first
    assert SOL_MINT in addrs          # URL one kept as second candidate


def test_parse_message_robinhood_link_not_solana():
    msg = RawMessage(channel_id=9, message_id=1,
                     text="Doge 1 retrace, listed on Gate Alpha.\n"
                          f"https://dexscreener.com/robinhood/{ADDR_POOL}",
                     timestamp=NOW)
    # robinhood-path links must NOT leak into the Solana parser
    assert parse_message(msg) is None


# ---- Bot-notification digests: URLs are dead for BOTH chains ---------------

def test_sol_tme_bot_start_deep_link_not_a_call():
    # Civilian Degens 'GROUPATH' leaderboard bot post: the 'called' token is
    # ONLY inside t.me/RickBurpBot?start=<mint> links — must yield nothing.
    text = ("🏆 Civillian Investors Talk 7D #GROUPATH\n"
            "[**TRIPLET**](https://t.me/RickBurpBot?start=0x33747DC366636AcEF03ca0C809a22A966e91bd1F)"
            " [**REVENGE**](https://t.me/RickBurpBot?start=bykrhkExmjWco2mFxKGX3XPmPVaZB9JJp9MkRe8pump)")
    assert extract_addresses(text) == []


def test_sol_achievement_unlocked_not_a_call():
    # 666/RamJ style notification: '@channel made a x2+ call on [Tok](t.me/bot?start=mint)'
    text = ("**Achievement Unlocked**: **x2!** @x666calls made a **x2+** call on "
            "[Marco](https://t.me/spydefi_bot?start=DgXjupqUXCMRzyR98WxKPdhxyLC72MdtMJULu4C3pump).")
    assert extract_addresses(text) == []


def test_sol_xcom_link_not_a_call():
    text = "Head of Engineering at Coinbase reposed Reeve https://x.com/someuser"
    assert extract_addresses(text) == []
