"""Regression tests: calls posted AS DEX links (Civilian Degens pattern).

Both parsers used to strip ALL URLs before extraction, so channels whose
posts are comment + dexscreener/gate links yielded almost no calls. Fix:
addresses inside a chain-scoped allowlist of DEX hosts are extracted; every
other URL (t.me bot-mirror params, explorer tx paths, wallet-profile hosts
like debank) stays dead — those regressions are tested explicitly here.
Cross-chain safety: a /bsc/ link parses as a BSC call (chain detected from
the path), never as robinhood — detect_chain assertions per path below.
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

def test_rh_bsc_url_is_a_bsc_call_not_robinhood():
    # (policy changed 2026-09-30, wifechangingcalls audit: /bsc//base//eth/
    # dexscreener links ARE calls — the EVM parser is the all-chain gatherer,
    # chain detection downstream makes the PATH SEGMENT the chain authority,
    # so a BSC link can never masquerade as a Robinhood call.)
    text = "Giggle academy. https://dexscreener.com/bsc/" + ADDR_BSC
    assert rh_addrs(text) == [ADDR_BSC.lower()]
    from chains.chain_detector import detect_chain
    assert detect_chain(text, ADDR_BSC) == "bsc"


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


# ---- Symbol hints: market-cap phrases are amounts, not tickers -------------

def test_rh_symbol_never_comes_from_message_text():
    # Ruling B (2026-09-30, RUFUS/NVDA + COON/PENPE lesson): the
    # last-$TICKER-before-address heuristic mislabels every channel that
    # compares coins in prose. EVM parses leave the symbol empty; the
    # canonical ticker arrives from the DATA SOURCE at pricing time
    # (token_meta / DexScreener / Birdeye identity -> apply_eval7d).
    text = ("The lead AI engineer at Treasure DAO ($MAGIC), which ran to a "
            "~$2B market cap, has launched $FLYAI. Sitting around a $300K "
            "market cap. 0x0088CE7905025c4B5ea1d49aB6179B6aaADB3B9C")
    calls = rh.parse_calls(1, 1, text, NOW)
    assert calls[0].token_address == "0x0088ce7905025c4b5ea1d49ab6179b6aaadb3b9c"
    assert calls[0].token_symbol is None


def test_rh_mint_beats_pool_link():
    # Ruling A (RUFUS): in-text backticked mint WINS over the dexscreener
    # pool URL; the pool address must never become the tracked token.
    pool = "0x" + "a1" * 32
    mint = "0x46B6995b02B1e3Afa39033243999e00d739615F1"
    text = (f"Gamboled a bag on $RUFUS. Paired with $NVDA.\n\n"
            f"https://dexscreener.com/robinhood/{pool}\n\n`{mint}`")
    calls = rh.parse_calls(1, 1, text, NOW)
    addrs = [c.token_address for c in calls]
    assert addrs == [mint.lower()]          # ONE call: the mint, not the pool


def test_rh_link_only_post_falls_back_to_url():
    # The URL path stays a fallback for link-only posts (no in-text mint).
    pool = "0x" + "b2" * 32
    text = f"New pair \n\n https://dexscreener.com/robinhood/{pool}"
    calls = rh.parse_calls(1, 1, text, NOW)
    assert [c.token_address for c in calls] == [pool]
    assert calls[0].token_symbol is None


def test_rh_arc_links_are_allowlisted():
    # Ruling A part 2 (Happy/$USDC): dexscreener /arc/ URLs count as
    # call venues too — link-only Arc posts must not be invisible.
    addr = "0x" + "c3" * 20
    text = f"call on arc\n\nhttps://dexscreener.com/arc/{addr}"
    calls = rh.parse_calls(1, 1, text, NOW)
    assert calls and calls[0].token_address == addr


# ---- gmgn.ai link calls (thecasinoeye audit, 2026-09-30: $PAR x300+ lost) ----

def test_gmgn_robinhood_token_link_is_a_call():
    # The channel's real Sept format: link-only 'Gambled here DYOR' post.
    text = ("Gambled some here **DYOR**\n\n"
            "https://gmgn.ai/robinhood/token/EYEKING_0xe3a7f023a4aa2a8e41232"
            "32c3de8ed5691d382da\n\nhttps://x.com/MeowRobinhood")
    calls = rh.parse_calls(1, 1, text, NOW)
    assert [c.token_address for c in calls] == [
        "0xe3a7f023a4aa2a8e4123232c3de8ed5691d382da"]


def test_gmgn_achievement_deeplink_stays_noise():
    # The spydefi 'x300+ Achievement Unlocked' reposts embed the SAME mint
    # as a t.me deep link — that host is NOT allowlisted and must never
    # create a call (window-wide dedup also keeps only msg 2683's entry).
    text = ("**Achievement Unlocked**: **Face Melter!** 🫠 @thecasinoeye "
            "made a **x300+** call on [par](https://t.me/spydefi_bot?start="
            "0x507b6f349a80114097a67b8b4677367acc15b220).")
    assert rh.parse_calls(1, 1, text, NOW) == []


def test_gmgn_sol_token_label_prefix_splits_to_mint():
    # eye1_<mint>: gmgn prefixes display labels with '_' — base58 mints
    # never contain '_', so the splitter recovers the real address only.
    from ingestion.address_parser import extract_addresses
    text = ("Gambled here **DYOR**\n\n"
            "https://gmgn.ai/sol/token/eye1_5wn857GFhKHA6f2dcFG8AQEzCD96Dk"
            "ZpiviV6csWD8vj\n\nhttps://x.com/PrivacyPadOnSOL")
    assert extract_addresses(text) == [
        "5wn857GFhKHA6f2dcFG8AQEzCD96DkZpiviV6csWD8vj"]


def test_gmgn_path_segment_drives_chain_detection():
    from chains.chain_detector import detect_chain_from_context
    addr = "0x" + "d4" * 20
    assert detect_chain_from_context(f"https://gmgn.ai/robinhood/token/EYEKING_{addr}", addr) == "robinhood"
    assert detect_chain_from_context(f"https://gmgn.ai/sol/token/eye1_{addr}", addr) is None  # sol is the solana parser's domain


def test_gmgn_arc_token_link_is_a_call():
    # completion of the gmgn ruling (a7af117): arc was in the dexscreener
    # alternation but missed in the gmgn one — tomleessonscalls (2026-09-15,
    # 'tailed wep here') posts calls as gmgn.ai/arc/token links.
    text = ("https://gmgn.ai/arc/token/0xc468a7117725722c163cef0f717414e0a7afeaa9\n\n"
            "tailed wep here")
    calls = rh.parse_calls(1, 1, text, NOW)
    assert [c.token_address for c in calls] == [
        "0xc468a7117725722c163cef0f717414e0a7afeaa9"]
    from chains.chain_detector import detect_chain_from_context
    assert detect_chain_from_context(text, "0xc468a7117725722c") == "arc"


def test_rh_base_and_eth_paths_route_to_their_chains():
    from chains.chain_detector import detect_chain
    base = "0x3ebbbaa60c309ec7d857a399051a5e20eb886215"
    text = "https://dexscreener.com/base/" + base
    assert rh_addrs(text) == [base.lower()]
    assert detect_chain(text, base) == "base"
    eth = "0x5c67b6dc46bbc8b16c7e72749e57138d22b9b3b2"
    t2 = "repeg https://dexscreener.com/eth/" + eth
    assert rh_addrs(t2) == [eth.lower()]
    assert detect_chain(t2, eth) == "eth"


def test_debank_wallet_profile_is_not_a_call():
    # (tomleessons/wife audit watch-item: debank profiles embed WALLET
    # addresses — a $50K buy on $SPIKE is commentary, not the token mint.)
    spike = "0xdbed41aaeb80854a2a744a60d9c721f0580e67e6"
    text = "50,000$ buy on $SPIKE\n\nhttps://debank.com/profile/" + spike
    assert rh_addrs(text) == []
