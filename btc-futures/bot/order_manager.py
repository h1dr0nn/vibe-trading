"""Place, cancel, and close orders on OKX.

All write operations are guarded by DRY_RUN — when enabled, actions are
logged only and state is updated with a dry_run flag.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from bot import okx_private as okx
from bot.okx_errors import OKXError, with_retry

logger = logging.getLogger(__name__)

CONTRACT_SIZE = 0.01  # BTC per contract for BTC-USDT-SWAP


def _dry_run() -> bool:
    return os.getenv("DRY_RUN", "true").lower() == "true"


def _inst_id() -> str:
    return os.getenv("BOT_SYMBOL", "BTC-USDT-SWAP")


# ── Position sizing ───────────────────────────────────────────────────────────

def calc_contracts(
    balance: float,
    risk_pct: float,
    entry_price: float,
    sl_price: float,
    leverage: int,
) -> int:
    """Calculate number of contracts to trade using risk-based sizing.

    Target size (BTC) = (balance × risk_pct%) / SL_distance_per_BTC.
    Contracts = target_BTC / CONTRACT_SIZE (0.01 BTC per contract).

    Leverage does NOT multiply size — it only controls margin required.
    A leverage check ensures margin ≤ 90% of balance.

    If target < 1 contract, round up to 1 *only* if 1-contract risk
    stays within MAX_RISK_PCT of balance. Otherwise return 0 (skip trade).
    """
    sl_dist = abs(entry_price - sl_price)
    if sl_dist == 0 or balance <= 0 or entry_price <= 0:
        logger.warning("calc_contracts: invalid inputs — returning 0")
        return 0

    max_risk_pct = float(os.getenv("MAX_RISK_PCT", "5.0"))
    target_risk_usdt = balance * risk_pct / 100
    hard_max_risk_usdt = balance * max_risk_pct / 100

    # Target contracts by risk%
    contracts_target = target_risk_usdt / (CONTRACT_SIZE * sl_dist)
    contracts = int(contracts_target)

    # Round sub-1 up to 1 iff within MAX_RISK_PCT
    if contracts < 1 and contracts_target > 0:
        one_contract_risk = CONTRACT_SIZE * sl_dist
        if one_contract_risk <= hard_max_risk_usdt:
            contracts = 1
            logger.info(
                "Sizing: target %.3f contracts < 1 — rounding up to 1 "
                "(risk=$%.2f = %.2f%% of balance, within %.1f%% cap)",
                contracts_target, one_contract_risk,
                one_contract_risk / balance * 100, max_risk_pct,
            )
        else:
            logger.warning(
                "Sizing: 1-contract risk $%.2f > MAX_RISK_PCT %.1f%% "
                "($%.2f) — skipping trade",
                one_contract_risk, max_risk_pct, hard_max_risk_usdt,
            )
            return 0

    # Margin sanity: notional / leverage ≤ 90% balance
    notional = contracts * CONTRACT_SIZE * entry_price
    margin_needed = notional / leverage if leverage > 0 else notional
    max_margin = balance * 0.9
    if margin_needed > max_margin and leverage > 0:
        scaled = int(max_margin * leverage / entry_price / CONTRACT_SIZE)
        logger.warning(
            "Sizing: margin $%.2f > 90%% balance — scaling %d -> %d contracts",
            margin_needed, contracts, scaled,
        )
        contracts = max(scaled, 0)

    actual_risk = contracts * CONTRACT_SIZE * sl_dist
    logger.info(
        "Sizing: balance=$%.2f risk=%.2f%% sl=%.2f%% → %d contracts "
        "(risk=$%.2f = %.2f%% · notional=$%.2f · margin=$%.2f)",
        balance, risk_pct, sl_dist / entry_price * 100, contracts,
        actual_risk, actual_risk / balance * 100 if balance else 0,
        contracts * CONTRACT_SIZE * entry_price,
        contracts * CONTRACT_SIZE * entry_price / leverage if leverage else 0,
    )
    return contracts


# ── Open new trade ────────────────────────────────────────────────────────────

def open_trade(
    state: dict[str, Any],
    signal: dict[str, Any],
    balance: float,
) -> dict[str, Any]:
    """Place a limit/market order + attach algo TP/SL.

    Updates state['pending_order'] and returns state.
    Does NOT write state to disk — caller is responsible for save_state().
    """
    inst_id = _inst_id()
    side = "buy" if signal["direction"] == 1 else "sell"
    entry_price = signal["entry"]
    sl_price = signal["sl"]
    tp1_price = signal["tp1"]
    tp2_price = signal.get("tp2")

    risk_pct = float(os.getenv("RISK_PCT", "1.0"))
    leverage = int(os.getenv("LEVERAGE", "5"))
    order_type = os.getenv("ORDER_TYPE", "limit")

    contracts = calc_contracts(balance, risk_pct, entry_price, sl_price, leverage)
    if contracts < 1:
        logger.warning("Calculated contracts < 1 — skipping trade")
        state["last_action"] = "skip_size_too_small"
        return state

    size_str = str(contracts)
    entry_str = f"{entry_price:.2f}"
    sl_str = f"{sl_price:.2f}"
    tp1_str = f"{tp1_price:.2f}"

    if _dry_run():
        logger.info(
            "DRY RUN: would place %s %s order @ %s size=%s contracts, "
            "TP1=%s SL=%s",
            order_type, side, entry_str, size_str, tp1_str, sl_str,
        )
        # Simulate pending order in state
        state["pending_order"] = {
            "active": True,
            "order_id": "DRY_" + datetime.now(tz=timezone.utc).strftime("%H%M%S"),
            "entry_price": entry_price,
            "placed_at": datetime.now(tz=timezone.utc).isoformat(),
            "side": side,
            "sl_price": sl_price,
            "tp1_price": tp1_price,
            "tp2_price": tp2_price,
            "original_sl_price": sl_price,
        }
        state["last_action"] = "dry_run_placed_order"
        state["last_signal"] = {
            "direction": signal["direction"],
            "confidence": signal.get("confidence"),
            "net_score": signal.get("net_score"),
            "source": signal.get("source", "local"),
        }
        return state

    # --- Live trading ---
    try:
        # Step 1: place order — save ordId to state immediately before algo
        ord_id = with_retry(
            okx.place_order,
            inst_id, side, size_str, order_type, entry_str,
        )
        state["pending_order"] = {
            "active": True,
            "order_id": ord_id,
            "entry_price": entry_price,
            "placed_at": datetime.now(tz=timezone.utc).isoformat(),
            "side": side,
            "sl_price": sl_price,
            "tp1_price": tp1_price,
            "tp2_price": tp2_price,
            "original_sl_price": sl_price,
        }
        # Caller should save state here before step 2
        # (main loop calls save_state after open_trade returns)

        # Step 2: attach algo TP/SL (standalone OCO on the instrument)
        algo_id = with_retry(
            okx.set_algo_tp_sl,
            inst_id, side, size_str, tp1_str, sl_str,
        )
        state["pending_order"]["algo_id"] = algo_id

        state["last_action"] = "placed_order"
        state["last_signal"] = {
            "direction": signal["direction"],
            "confidence": signal.get("confidence"),
            "net_score": signal.get("net_score"),
            "source": signal.get("source", "local"),
        }

        logger.info(
            "Trade opened: %s %s @ %s size=%s ordId=%s algoId=%s",
            order_type, side, entry_str, size_str, ord_id, algo_id,
        )

    except OKXError as exc:
        logger.error("open_trade failed: %s", exc)
        raise

    return state


# ── Close position ────────────────────────────────────────────────────────────

def close_trade(
    state: dict[str, Any],
    reason: str,
    current_price: float | None = None,
) -> dict[str, Any]:
    """Close current open position via market order.

    Steps: cancel algos → cancel pending orders → close position.
    Updates state and returns it.
    """
    inst_id = _inst_id()
    pos = state.get("position", {})

    if _dry_run():
        logger.info("DRY RUN: would close position (reason=%s)", reason)
        _clear_position(state, reason, current_price)
        state["last_action"] = "dry_run_closed"
        return state

    # Step 1: cancel algo TP/SL
    algo_id = pos.get("algo_order_id")
    if algo_id:
        try:
            with_retry(okx.cancel_algo, inst_id, algo_id)
        except OKXError as exc:
            logger.warning("Could not cancel algo %s: %s", algo_id, exc)

    # Step 2: cancel pending limit order (if any)
    pend_ord_id = state["pending_order"].get("order_id")
    if state["pending_order"].get("active") and pend_ord_id:
        try:
            with_retry(okx.cancel_order, inst_id, pend_ord_id)
        except OKXError as exc:
            logger.warning("Could not cancel pending order %s: %s", pend_ord_id, exc)

    # Step 3: close position
    try:
        with_retry(okx.close_position, inst_id)
    except OKXError as exc:
        logger.error("close_position failed: %s", exc)
        raise

    # Record realised PnL before clearing state
    if current_price is not None and pos.get("active"):
        entry = pos.get("entry_price") or 0
        size = pos.get("size_contracts") or 0
        direction = 1 if pos.get("side") == "long" else -1
        realized = size * CONTRACT_SIZE * (current_price - entry) * direction
        from bot import circuit_breaker as _cb
        _cb.record_pnl(state, realized)
        logger.info("Recorded realized PnL: $%.4f (reason=%s)", realized, reason)

        # Remember last winning close so pullback gate blocks fomo
        # re-entry (applies to any profitable close, not just TP1).
        if realized > 0:
            state["last_tp_close"] = {
                "side": pos.get("side"),
                "close_price": current_price,
                "time": datetime.now(tz=timezone.utc).isoformat(),
            }
        # Mirror: remember last losing close so loss-cooldown gate blocks
        # revenge re-entry in the same direction.
        elif realized < 0:
            state["last_loss_close"] = {
                "side": pos.get("side"),
                "close_price": current_price,
                "time": datetime.now(tz=timezone.utc).isoformat(),
            }

    _clear_position(state, reason, current_price)
    state["last_action"] = "closed_position"
    logger.info("Position closed (reason=%s)", reason)
    return state


# ── Attach TP/SL to existing position ────────────────────────────────────────

def attach_tp_sl(
    state: dict[str, Any],
) -> dict[str, Any]:
    """Set algo TP/SL for an existing open position that has none.

    Used when bot restarts and finds IN_POSITION_NO_SL state.
    """
    inst_id = _inst_id()
    pos = state["position"]
    side = pos["side"]
    tp1 = pos.get("tp1_price")
    sl = pos.get("sl_price")

    if not tp1 or not sl:
        logger.warning("attach_tp_sl: TP1 or SL price missing in state")
        return state

    tp1_str = f"{tp1:.2f}"
    sl_str = f"{sl:.2f}"

    if _dry_run():
        logger.info("DRY RUN: would attach algo TP=%s SL=%s", tp1_str, sl_str)
        state["last_action"] = "dry_run_attached_tp_sl"
        return state

    try:
        algo_id = with_retry(
            okx.set_algo_tp_sl_for_position,
            inst_id, "buy" if side == "long" else "sell",
            tp1_str, sl_str,
        )
        state["position"]["algo_order_id"] = algo_id
        state["last_action"] = "attached_tp_sl"
        logger.info("Attached TP/SL algoId=%s", algo_id)
    except OKXError as exc:
        logger.error("attach_tp_sl failed: %s", exc)
        raise

    return state


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clear_position(
    state: dict[str, Any],
    reason: str,
    close_price: float | None,
) -> None:
    state["position"] = {
        "active": False,
        "side": None,
        "entry_price": None,
        "size_contracts": None,
        "open_time": None,
        "sl_price": None,
        "tp1_price": None,
        "tp2_price": None,
        "algo_order_id": None,
        "entry_order_id": None,
        "reconciled": False,
        "dry_run": _dry_run(),
        "original_sl_price": None,
        "trail_stage": 0,
        "trail_reason": None,
    }
    state["pending_order"] = {
        "active": False,
        "order_id": None,
        "entry_price": None,
        "placed_at": None,
        "side": None,
    }
    state["last_action"] = reason
