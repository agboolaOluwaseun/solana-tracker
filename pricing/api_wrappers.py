"""
Pricing wrapper functions for Birdeye and GeckoTerminal.
Provides simple, consistent interface for pricing calls.
"""
import sys
sys.path.insert(0, "/Users/agboolaoluwaseun/ZCodeProject/solana_tracker_v2")

from datetime import datetime, timezone
from db import init_db, get_connection, transaction
from pricing.backtest import backtest_call_birdeye, backtest_call
from pricing.geckoterminal import GeckoTerminalClient
from pricing.birdeye import BirdeyeClient
from models import ParsedCall


def birdeye_api(call: ParsedCall, call_ts: datetime) -> dict:
    """
    Price a call using Birdeye API.
    
    Args:
        call: ParsedCall object with token_address, token_symbol
        call_ts: Timestamp when the call was made (naive UTC or timezone-aware)
    
    Returns:
        dict with keys: success, entry_price, peak_price, peak_timestamp, 
                       peak_multiple, is_win, status, error
    """
    try:
        # Convert to naive UTC if timezone-aware
        if call_ts.tzinfo is not None:
            call_ts = call_ts.replace(tzinfo=None)
        
        client = BirdeyeClient()
        result, sl_result = backtest_call_birdeye(
            client=client,
            token_address=call.token_address,
            call_ts=call_ts,
        )
        
        if result is None:
            return {
                "success": False,
                "status": "unpriceable_loss",
                "error": "Birdeye returned no data",
            }
        
        # Compute peak_multiple from peak_profit_pct: multiple = (pct/100) + 1
        peak_multiple = (result.peak_profit_pct / 100 + 1) if result.peak_profit_pct is not None else None
        
        return {
            "success": True,
            "entry_price": result.entry_price_usd,
            "peak_price": result.peak_price_usd,
            "peak_timestamp": result.peak_timestamp,
            "peak_profit_pct": result.peak_profit_pct,
            "peak_multiple": peak_multiple,
            "is_win": result.is_win,
            "status": "win" if result.is_win else "loss",
            "error": None,
        }
    except Exception as e:
        return {
            "success": False,
            "status": "unpriceable_loss",
            "error": str(e),
        }


def geckoterminal_api(call: ParsedCall, call_ts: datetime) -> dict:
    """
    Price a call using GeckoTerminal API.
    
    Args:
        call: ParsedCall object with token_address, token_symbol
        call_ts: Timestamp when the call was made (naive UTC or timezone-aware)
    
    Returns:
        dict with keys: success, entry_price, peak_price, peak_timestamp, 
                       peak_profit_pct, is_win, status, error
    """
    try:
        # Convert to naive UTC if timezone-aware
        if call_ts.tzinfo is not None:
            call_ts = call_ts.replace(tzinfo=None)
        
        client = GeckoTerminalClient()
        result, sl_result = backtest_call(
            client=client,
            token_address=call.token_address,
            call_ts=call_ts,
        )
        
        if result is None:
            return {
                "success": False,
                "status": "unpriceable_loss",
                "error": "GeckoTerminal returned no data",
            }
        
        # Compute peak_multiple from peak_profit_pct: multiple = (pct/100) + 1
        peak_multiple = (result.peak_profit_pct / 100 + 1) if result.peak_profit_pct is not None else None
        
        return {
            "success": True,
            "entry_price": result.entry_price_usd,
            "peak_price": result.peak_price_usd,
            "peak_timestamp": result.peak_timestamp,
            "peak_profit_pct": result.peak_profit_pct,
            "peak_multiple": peak_multiple,
            "is_win": result.is_win,
            "status": "win" if result.is_win else "loss",
            "error": None,
        }
    except Exception as e:
        return {
            "success": False,
            "status": "unpriceable_loss",
            "error": str(e),
        }


def price_call_with_api(call: ParsedCall, call_ts: datetime, api: str = "birdeye") -> dict:
    """
    Price a call using the specified API.
    
    Args:
        call: ParsedCall object
        call_ts: Call timestamp
        api: "birdeye" or "geckoterminal"
    
    Returns:
        Pricing result dict
    """
    if api == "birdeye":
        return birdeye_api(call, call_ts)
    elif api == "geckoterminal":
        return geckoterminal_api(call, call_ts)
    else:
        raise ValueError(f"Unknown API: {api}")


def persist_price_result(call_id: int, result: dict) -> None:
    """
    Persist pricing result to database.
    
    Args:
        call_id: Database ID of the call
        result: Pricing result dict from birdeye_api or geckoterminal_api
    """
    init_db()
    with transaction() as conn:
        if result["success"]:
            conn.execute(
                """UPDATE calls SET
                   entry_price_usd = ?,
                   peak_price_usd = ?,
                   peak_timestamp = ?,
                   peak_multiple = ?,
                   is_win = ?,
                   status = ?,
                   priced_at = CURRENT_TIMESTAMP,
                   error = NULL
                   WHERE id = ?""",
                (
                    result["entry_price"],
                    result["peak_price"],
                    result["peak_timestamp"],
                    result["peak_multiple"],
                    1 if result["is_win"] else 0,
                    result["status"],
                    call_id,
                ),
            )
        else:
            conn.execute(
                """UPDATE calls SET
                   status = ?,
                   error = ?,
                   priced_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (result["status"], result["error"], call_id),
            )
