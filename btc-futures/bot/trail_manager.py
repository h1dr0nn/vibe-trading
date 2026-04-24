"""Progressive SL trail — move SL to breakeven then lock profit as R grows.

Stages (R = |entry - original_sl|):
    0 → initial SL (unchanged)
    1 → breakeven: once +BE_TRIGGER_R reached, SL = entry + BE_BUFFER_R × R
    2 → locked:   once +TRAIL_TRIGGER_R reached, SL = entry + TRAIL_LOCK_R × R

Live mode: cancels the existing algo OCO and re-creates with new SL
(TP1 unchanged) via set_algo_tp_sl_for_position.

Dry-run mode: updates state only.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _dry_run() -> bool:
    return os.getenv("DRY_RUN", "true").lower() == "true"


def _inst_id() -> str:
    return os.getenv("BOT_SYMBOL", "BTC-USDT-SWAP")


def manage_trail(
    state: dict[str, Any],
    current_price: float,
) -> tuple[dict[str, Any], bool, str]:
    """Advance SL trail if the price has earned the next stage.

    Returns (state, moved, reason). When `moved` is True, state has been
    mutated with new sl_price / trail_stage / algo_order_id and caller
    should save_state() and notify.
    """
    pos = state.get("position", {})
    if not pos.get("active"):
        return state, False, ""

    entry = pos.get("entry_price") or 0
    original_sl = pos.get("original_sl_price") or pos.get("sl_price") or 0
    current_sl = pos.get("sl_price") or original_sl
    side = pos.get("side")
    stage = int(pos.get("trail_stage") or 0)
    tp1 = pos.get("tp1_price")

    if not entry or not original_sl or not tp1 or not side:
        return state, False, ""

    r_dist = abs(entry - original_sl)
    if r_dist <= 0 or stage >= 2:
        return state, False, ""

    dir_sign = 1 if side == "long" else -1
    profit_r = (current_price - entry) * dir_sign / r_dist

    be_trigger = _env_float("BE_TRIGGER_R", 1.0)
    be_buffer = _env_float("BE_BUFFER_R", 0.1)
    trail_trigger = _env_float("TRAIL_TRIGGER_R", 1.5)
    trail_lock = _env_float("TRAIL_LOCK_R", 1.0)

    new_sl: float | None = None
    new_stage = stage
    reason = ""

    # Prefer the higher stage if both triggers have been earned at once
    if stage < 2 and profit_r >= trail_trigger:
        new_sl = entry + dir_sign * trail_lock * r_dist
        new_stage = 2
        reason = f"lock +{trail_lock:g}R at {profit_r:.2f}R"
    elif stage < 1 and profit_r >= be_trigger:
        new_sl = entry + dir_sign * be_buffer * r_dist
        new_stage = 1
        reason = f"breakeven +{be_buffer:g}R at {profit_r:.2f}R"

    if new_sl is None:
        return state, False, ""

    # Only advance if the new SL is strictly better than current (no regress)
    if dir_sign == 1 and new_sl <= current_sl:
        return state, False, ""
    if dir_sign == -1 and new_sl >= current_sl:
        return state, False, ""

    # Replace the algo OCO on the exchange (live) — cancel old, create new
    if not _dry_run():
        ok = _replace_algo(state, new_sl, tp1, side)
        if not ok:
            return state, False, ""

    state["position"]["sl_price"] = round(new_sl, 2)
    state["position"]["trail_stage"] = new_stage
    state["position"]["trail_reason"] = reason
    logger.info(
        "Trail advanced: stage %d→%d, SL %.2f→%.2f (profit=%.2fR, %s)",
        stage, new_stage, current_sl, new_sl, profit_r, reason,
    )
    return state, True, reason


def _replace_algo(
    state: dict[str, Any],
    new_sl: float,
    tp1: float,
    side: str,
) -> bool:
    """Cancel existing algo and re-attach OCO with new SL. Returns success."""
    from bot import okx_private as okx
    from bot.okx_errors import OKXError, with_retry

    inst_id = _inst_id()
    old_algo_id = state["position"].get("algo_order_id")

    if old_algo_id:
        try:
            with_retry(okx.cancel_algo, inst_id, old_algo_id)
        except OKXError as exc:
            # Log but continue — the algo may have already fired; the next
            # reconciler cycle will detect the external close if that's the case.
            logger.warning("Trail: cancel_algo %s failed — %s", old_algo_id, exc)

    entry_side = "buy" if side == "long" else "sell"
    try:
        new_algo_id = with_retry(
            okx.set_algo_tp_sl_for_position,
            inst_id, entry_side, f"{tp1:.2f}", f"{new_sl:.2f}",
        )
    except OKXError as exc:
        logger.error("Trail: set_algo_tp_sl_for_position failed — %s", exc)
        return False

    state["position"]["algo_order_id"] = new_algo_id
    return True
