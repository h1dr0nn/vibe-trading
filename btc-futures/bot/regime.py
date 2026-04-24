"""Market regime classifier for the 4H timeframe.

Classifies the current market state into one of:
    - "trending_up"   — strong directional momentum up (ADX>25, EMA bull, BB wide)
    - "trending_down" — strong directional momentum down (ADX>25, EMA bear, BB wide)
    - "ranging"       — low ADX but stable inside Bollinger Bands
    - "choppy"        — indecisive, low ADX + tight bands + EMA cross flipping

Entry trades are blocked when regime == "choppy" unless BLOCK_CHOPPY is disabled.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def classify(tf_scores: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Classify 4H regime using a small set of indicator heuristics.

    Returns:
        {
          "regime": str,
          "block_entry": bool,
          "reason": str,
        }
    """
    s4h = tf_scores.get("4H") or {}
    s1h = tf_scores.get("1H") or {}

    adx_4h = float(s4h.get("adx", 0) or 0)
    adx_1h = float(s1h.get("adx", 0) or 0)
    bb_pct_4h = float(s4h.get("bb_pct", 0.5) or 0.5)
    ema_cross_4h = s4h.get("ema_cross", "")

    adx_trend_min = 25.0
    adx_choppy_max = 23.0  # 1H ADX below this = entry TF non-trending (catches 21-22)

    # Strong trend: ADX high on 4H AND EMA aligned
    if adx_4h >= adx_trend_min:
        if ema_cross_4h == "bull":
            return {
                "regime": "trending_up",
                "block_entry": False,
                "reason": f"4H ADX {adx_4h:.0f}≥{adx_trend_min:.0f}, EMA bull",
            }
        if ema_cross_4h == "bear":
            return {
                "regime": "trending_down",
                "block_entry": False,
                "reason": f"4H ADX {adx_4h:.0f}≥{adx_trend_min:.0f}, EMA bear",
            }

    # Choppy: entry-TF (1H) ADX weak AND price sitting mid-BB on 4H.
    # Blocking on 1H alone (not requiring 4H also low) catches the
    # common "4H trending but 1H chopping the range" whipsaw setup.
    if adx_1h < adx_choppy_max and 0.3 <= bb_pct_4h <= 0.7:
        return {
            "regime": "choppy",
            "block_entry": True,
            "reason": (
                f"1H ADX {adx_1h:.0f} < {adx_choppy_max:.0f} "
                f"(entry TF non-trending), BB% {bb_pct_4h:.2f} mid-range"
            ),
        }

    # Ranging: low ADX on both TFs but BB not mid — don't block,
    # just note it so downstream can weigh confidence.
    if adx_4h < adx_trend_min and adx_1h < adx_trend_min:
        return {
            "regime": "ranging",
            "block_entry": False,
            "reason": f"4H ADX {adx_4h:.0f}, 1H ADX {adx_1h:.0f} (low trend strength)",
        }

    # Fall-through: weak trend — not blocking but note weakness
    return {
        "regime": "weak_trend",
        "block_entry": False,
        "reason": f"4H ADX {adx_4h:.0f} (weak directional signal)",
    }
