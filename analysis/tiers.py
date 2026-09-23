"""Cumulative performance tiers for the KOLfi ranking meter.

Tier semantics (plan v2, Task B4):
  * A decided call lands in the HIGHEST tier its multiple reaches.
    Tiers are cumulative: 100x subset of 50x subset of ... subset of 2x.
  * '<2x' is the complement (decided calls that never reached 2x).
  * Multiple used per call: COALESCE(max_multiple, peak_multiple,
    1 + peak_profit_pct/100) so both 7d-engine rows and legacy 12h rows are
    tierable — legacy rows ported without a multiple column still carry
    their peak profit PERCENT, which converts to the same multiple scale.
    (Without that third fallback every legacy call silently counted as <2x,
    so the X2 bar contradicted the win rate.)
  * 'decided' = plain strategy win/loss (pending/unpriceable excluded).
  * Meter follows the selected timeframe; strategy='stoploss' restricts the
    population to calls the stoploss strategy decided (sr.status win/loss).

Invariant enforced by tests:
    count(100x) <= count(50x) <= ... <= count(2x)
    count(2x) + count(<2x) == total_decided
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from db import get_connection

from analysis.windowed import _iso, _strategy_join, window_since

# (label, lower bound on multiple). Descending — cumulative "reached >= X".
TIER_BOUNDS = [("100x", 100.0), ("50x", 50.0), ("25x", 25.0), ("10x", 10.0),
               ("5x", 5.0), ("3x", 3.0), ("2x", 2.0)]
TIER_LABELS = [b[0] for b in TIER_BOUNDS] + ["<2x"]


def tier_counts(channel_id: int, window: str, strategy: str = "normal",
                chain: Optional[str] = "all",
                since: Optional[datetime] = None) -> Dict:
    """Cumulative tier counts/percentages for one channel in one timeframe.

    `since` (naive UTC datetime) overrides `window` when given — the API's
    days-pills (1|3|7|30) resolve to a concrete cutoff and pass it here.

    Returns {"total_decided": N,
             "tiers": [{"label": "100x", "count": c, "pct": p}, ...],
             "wins": W, "win_rate": pct|None}
    """
    if since is None:
        since = window_since(window)
    conn = get_connection()
    chain_sql, chain_params = (
        (" AND cal.chain = ?", [chain]) if chain not in (None, "all") else ("", [])
    )
    if strategy in ("stoploss", "trailing"):
        q = f"""
            SELECT COALESCE(cal.max_multiple, cal.peak_multiple,
                            1.0 + cal.peak_profit_pct / 100.0) AS m,
                   sr.is_win AS w
            FROM calls cal {_strategy_join(strategy)}
            WHERE cal.channel_id = ?{chain_sql}
        """
    else:
        q = f"""
            SELECT COALESCE(cal.max_multiple, cal.peak_multiple,
                            1.0 + cal.peak_profit_pct / 100.0) AS m,
                   cal.is_win AS w
            FROM calls cal
            WHERE cal.channel_id = ?{chain_sql} {_strategy_join(strategy)}
        """
    params: list = [channel_id] + chain_params
    if since:
        q += " AND cal.call_timestamp >= ?"
        params.append(_iso(since))
    rows = conn.execute(q, params).fetchall()

    multiples: List[Optional[float]] = []
    wins = 0
    for r in rows:
        m = r["m"]
        if m is not None:
            m = float(m)
        multiples.append(m)
        wins += int(r["w"] or 0)

    total = len(multiples)
    tiers: List[Dict] = []
    for label, bound in TIER_BOUNDS:
        # A call counts for tier X if its best multiple >= X (cumulative).
        c = sum(1 for m in multiples if m is not None and m >= bound)
        tiers.append({
            "label": label,
            "count": c,
            "pct": (c / total * 100.0) if total else 0.0,
        })
    lt2 = sum(1 for m in multiples if m is None or m < 2.0)
    tiers.append({
        "label": "<2x",
        "count": lt2,
        "pct": (lt2 / total * 100.0) if total else 0.0,
    })
    return {
        "channel_id": channel_id,
        "window": window,
        "strategy": strategy,
        "chain": chain or "all",
        "total_decided": total,
        "wins": wins,
        "win_rate": (wins / total * 100.0) if total else None,
        "tiers": tiers,
    }
