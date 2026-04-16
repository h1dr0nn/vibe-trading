"""Position danger scoring and size synchronisation.

Evaluates 5 danger conditions per BOT.md Section 7.3:
  1. Price too close to SL (< DANGER_SL_ATR_MULT × ATR_1H)
  2. Strong signal flip (multi-TF direction reversed, confidence >= 70%)
  3. Extreme funding rate
  4. Position held too long (> MAX_OPEN_HOURS)
  5. Floating loss too large (> DANGER_LOSS_PCT% of balance)
"""

from __future__ import annotations

import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Make project root importable
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import indicators
import okx_client

logger = logging.getLogger(__name__)


def sync_position_size(state: dict[str, Any]) -> dict[str, Any]:
    """Fetch live position from OKX and reconcile size in state.

    Updates state['position']['size_contracts'] if partial fill occurred.
    Marks position inactive if fully closed.
    Returns state (mutated in-place).
    """
    if os.getenv("DRY_RUN", "true").lower() == "true":
        return state

    if not state["position"]["active"]:
        return state

    from bot import okx_private as okx

    inst_id = os.getenv("BOT_SYMBOL", "BTC-USDT-SWAP")
    try:
        live = okx.get_position(inst_id)
    except Exception as exc:
        logger.warning("sync_position_size: could not fetch OKX position — %s", exc)
        return state

    if live is None:
        # Position closed (TP/SL hit)
        logger.info("sync_position_size: position closed on OKX (TP/SL hit or manual)")
        state["position"]["active"] = False
        state["last_action"] = "position_closed_externally"
        return state

    live_size = abs(float(live.get("pos", 0)))
    stored_size = state["position"].get("size_contracts", 0) or 0

    if live_size == 0:
        state["position"]["active"] = False
        state["last_action"] = "position_closed_externally"
        logger.info("sync_position_size: live size=0, position closed")
    elif abs(live_size - stored_size) > 0.0001:
        logger.info(
            "sync_position_size: partial fill detected %.4f -> %.4f contracts",
            stored_size, live_size,
        )
        state["position"]["size_contracts"] = live_size

    return state


def evaluate_danger(
    state: dict[str, Any],
    current_price: float,
    balance: float,
) -> tuple[bool, list[str]]:
    """Evaluate all 5 danger conditions.

    Returns (is_dangerous: bool, reasons: list[str]).
    """
    pos = state.get("position", {})
    if not pos.get("active"):
        return False, []

    inst_id = os.getenv("BOT_SYMBOL", "BTC-USDT-SWAP")
    symbol = inst_id.replace("-SWAP", "")  # BTC-USDT for OHLCV

    danger = False
    reasons: list[str] = []

    # ── Condition 1: Price close to SL ────────────────────────────────────────
    sl_price = pos.get("sl_price")
    if sl_price:
        atr_1h = _fetch_atr_1h(symbol)
        if atr_1h and atr_1h > 0:
            danger_mult = float(os.getenv("DANGER_SL_ATR_MULT", "0.5"))
            sl_distance = abs(current_price - sl_price)
            threshold = danger_mult * atr_1h
            if sl_distance < threshold:
                danger = True
                reasons.append(
                    f"Price ${current_price:,.0f} within {sl_distance:.0f} of SL ${sl_price:,.0f} "
                    f"(threshold={threshold:.0f} = {danger_mult}×ATR1H)"
                )

    # ── Condition 2: Strong signal flip ───────────────────────────────────────
    if os.getenv("DANGER_SIGNAL_FLIP", "true").lower() == "true":
        try:
            tf_result = _run_local_analysis(symbol)
            pos_dir = 1 if pos.get("side") == "long" else -1
            flip_dir = tf_result.get("signal", 0)
            flip_conf = tf_result.get("confidence_pct", 0)  # 0–100

            if flip_dir != 0 and flip_dir != pos_dir and flip_conf >= 70:
                danger = True
                flip_label = "LONG" if flip_dir == 1 else "SHORT"
                reasons.append(
                    f"Signal flip: {flip_label} @ {flip_conf}% confidence (opposing position)"
                )
        except Exception as exc:
            logger.warning("Condition 2 (signal flip) check failed: %s", exc)

    # ── Condition 3: Extreme funding rate ─────────────────────────────────────
    try:
        swap_id = inst_id if inst_id.endswith("-SWAP") else inst_id + "-SWAP"
        funding_data = okx_client.get_funding_rate(swap_id)
        funding_rate = float(funding_data.get("fundingRate", 0))
        pos_side = pos.get("side")
        if pos_side == "long" and funding_rate > 0.001:
            danger = True
            reasons.append(f"Extreme funding {funding_rate*100:.4f}%/8h (LONG pays)")
        elif pos_side == "short" and funding_rate < -0.001:
            danger = True
            reasons.append(f"Extreme funding {funding_rate*100:.4f}%/8h (SHORT pays)")
    except Exception as exc:
        logger.warning("Condition 3 (funding) check failed: %s", exc)

    # ── Condition 4: Held too long ────────────────────────────────────────────
    max_hours = float(os.getenv("MAX_OPEN_HOURS", "48"))
    open_time_str = pos.get("open_time")
    if open_time_str:
        open_time = _parse_dt(open_time_str)
        if open_time:
            hours_open = (datetime.now(tz=timezone.utc) - open_time).total_seconds() / 3600
            if hours_open > max_hours:
                danger = True
                reasons.append(f"Held {hours_open:.1f}h > max {max_hours}h")

    # ── Condition 5: Floating loss too large ──────────────────────────────────
    if balance > 0:
        entry = pos.get("entry_price", 0) or 0
        size = pos.get("size_contracts", 0) or 0
        if entry and size and current_price:
            # PnL in USDT: contracts × contract_size × (close - entry) × direction
            from bot.order_manager import CONTRACT_SIZE
            direction = 1 if pos.get("side") == "long" else -1
            unrealized_pnl = size * CONTRACT_SIZE * (current_price - entry) * direction
            pnl_pct = unrealized_pnl / balance * 100
            danger_loss = float(os.getenv("DANGER_LOSS_PCT", "2.0"))
            if pnl_pct < -danger_loss:
                danger = True
                reasons.append(
                    f"Floating loss {pnl_pct:.2f}% < -{danger_loss}% of balance "
                    f"(${unrealized_pnl:.2f} USDT)"
                )

    if danger:
        logger.warning("Position DANGEROUS — %d conditions: %s", len(reasons), "; ".join(reasons))
    else:
        logger.info("Position safe (checked %d conditions)", 5)

    return danger, reasons


def has_algo_tp_sl(state: dict[str, Any]) -> bool:
    """Return True if the open position already has algo TP/SL set."""
    if not state["position"]["active"]:
        return False

    if os.getenv("DRY_RUN", "true").lower() == "true":
        return bool(state["position"].get("algo_order_id"))

    from bot import okx_private as okx
    inst_id = os.getenv("BOT_SYMBOL", "BTC-USDT-SWAP")
    try:
        algos = okx.get_algo_pending(inst_id)
        return len(algos) > 0
    except Exception as exc:
        logger.warning("has_algo_tp_sl: could not check algos — %s", exc)
        # Fall back to state
        return bool(state["position"].get("algo_order_id"))


# ── Internal helpers ──────────────────────────────────────────────────────────

def _fetch_atr_1h(symbol: str) -> float | None:
    """Fetch 1H OHLCV and compute ATR."""
    try:
        df = okx_client.get_ohlcv(symbol, bar="1H", limit=50)
        if df.empty:
            return None
        tech = indicators.compute_all(df)
        return float(tech.iloc[-1]["atr"])
    except Exception as exc:
        logger.warning("_fetch_atr_1h failed: %s", exc)
        return None


def _run_local_analysis(symbol: str) -> dict:
    """Run multi-TF local analysis and return confluence result.

    Returns dict with keys: signal (-1/0/1), confidence_pct (0-100).
    Re-uses the scoring functions from commands/trade.py.
    """
    from commands.trade import TIMEFRAMES, _confluence, _score_tf

    results: dict = {}

    def fetch(key: str, bar: str) -> None:
        try:
            results[key] = okx_client.get_ohlcv(symbol, bar=bar, limit=300)
        except Exception as exc:
            logger.warning("_run_local_analysis: fetch %s failed — %s", key, exc)
            results[key] = None

    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [
            pool.submit(fetch, tf, tf) for tf in TIMEFRAMES
        ]
        for f in as_completed(futs):
            pass

    tf_scores: dict = {}
    for tf in TIMEFRAMES:
        df = results.get(tf)
        if df is None or df.empty:
            logger.warning("Missing OHLCV for %s — skipping signal flip check", tf)
            return {"signal": 0, "confidence_pct": 0}
        tf_scores[tf] = _score_tf(df)

    conf = _confluence(tf_scores)
    # Map "HIGH"/"MEDIUM"/"LOW" to numeric %
    conf_map = {"HIGH": 90, "MEDIUM": 70, "LOW": 40}
    confidence_pct = conf_map.get(conf["confidence"], 0) if conf["signal"] != 0 else 0
    return {
        "signal": conf["signal"],
        "confidence_pct": confidence_pct,
        "agreeing_tfs": conf["agreeing_tfs"],
    }


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
