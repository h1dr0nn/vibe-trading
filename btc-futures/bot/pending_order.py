"""Manage the lifecycle of a pending limit order.

Cancels the order when:
  - It has been open longer than PENDING_CANCEL_HOURS
  - Price has drifted more than PENDING_CANCEL_DRIFT_PCT% from the entry
  - The order was filled (transitions state to IN_POSITION)
  - The order was cancelled externally
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from bot import okx_private as okx
from bot.okx_errors import OKXError, OrderNotFoundError, with_retry

logger = logging.getLogger(__name__)


def manage(
    state: dict[str, Any],
    current_price: float,
) -> dict[str, Any]:
    """Check and act on the pending order each cycle.

    Returns state (mutated in-place).
    Does NOT write state to disk — caller is responsible.
    """
    if not state["pending_order"]["active"]:
        return state

    if os.getenv("DRY_RUN", "true").lower() == "true":
        return _manage_dry_run(state, current_price)

    inst_id = os.getenv("BOT_SYMBOL", "BTC-USDT-SWAP")
    ord_id = state["pending_order"]["order_id"]

    # Fetch current order status from OKX
    try:
        order = with_retry(okx.get_order, inst_id, ord_id)
    except OrderNotFoundError:
        logger.warning("Pending order %s not found on OKX — cleared from state", ord_id)
        _cancel_pending(state, "order_not_found_on_okx")
        return state
    except OKXError as exc:
        logger.warning("Could not fetch order %s: %s — will retry next cycle", ord_id, exc)
        return state

    if order is None:
        _cancel_pending(state, "order_not_found_on_okx")
        return state

    order_state = order.get("state", "")

    # ── Filled ────────────────────────────────────────────────────────────────
    if order_state == "filled":
        logger.info("Pending order %s filled → activating position", ord_id)
        _activate_position(state, order)
        return state

    # ── Cancelled externally ──────────────────────────────────────────────────
    if order_state in ("canceled", "partially_canceled"):
        filled_sz = float(order.get("accFillSz", 0))
        if filled_sz > 0:
            logger.info("Order %s partially filled (%.4f) — activating position", ord_id, filled_sz)
            _activate_position(state, order, partial=True)
        else:
            logger.warning("Order %s was cancelled externally — resetting to IDLE", ord_id)
            _cancel_pending(state, "cancelled_externally")
        return state

    # ── Timeout check ─────────────────────────────────────────────────────────
    cancel_hours = float(os.getenv("PENDING_CANCEL_HOURS", "6"))
    placed_at = _parse_dt(state["pending_order"].get("placed_at"))
    if placed_at:
        hours_open = (datetime.now(tz=timezone.utc) - placed_at).total_seconds() / 3600
        if hours_open >= cancel_hours:
            logger.info("Pending order %s timed out after %.1fh — cancelling", ord_id, hours_open)
            _do_cancel(inst_id, ord_id, state, f"timeout_{hours_open:.1f}h")
            return state

    # ── Price drift check ─────────────────────────────────────────────────────
    drift_pct_limit = float(os.getenv("PENDING_CANCEL_DRIFT_PCT", "0.5"))
    entry_price = state["pending_order"].get("entry_price", 0)
    if entry_price and current_price:
        drift_pct = abs(current_price - entry_price) / entry_price * 100
        if drift_pct >= drift_pct_limit:
            logger.info(
                "Pending order %s: price drifted %.2f%% from entry — cancelling",
                ord_id, drift_pct,
            )
            _do_cancel(inst_id, ord_id, state, f"price_drift_{drift_pct:.2f}pct")
            return state

    logger.info(
        "Pending order %s: still open (state=%s hours=%.1f)",
        ord_id,
        order_state,
        (datetime.now(tz=timezone.utc) - placed_at).total_seconds() / 3600 if placed_at else 0,
    )
    return state


# ── Dry run simulation ────────────────────────────────────────────────────────

def _manage_dry_run(state: dict[str, Any], current_price: float) -> dict[str, Any]:
    """Simulate pending order management in dry run mode."""
    ord_id = state["pending_order"]["order_id"]
    entry_price = state["pending_order"].get("entry_price", 0)
    placed_at = _parse_dt(state["pending_order"].get("placed_at"))

    cancel_hours = float(os.getenv("PENDING_CANCEL_HOURS", "6"))
    drift_pct_limit = float(os.getenv("PENDING_CANCEL_DRIFT_PCT", "0.5"))

    if placed_at:
        hours_open = (datetime.now(tz=timezone.utc) - placed_at).total_seconds() / 3600
        if hours_open >= cancel_hours:
            logger.info("DRY RUN: pending order %s timed out — would cancel", ord_id)
            _cancel_pending(state, f"dry_run_timeout_{hours_open:.1f}h")
            return state

    if entry_price and current_price:
        drift_pct = abs(current_price - entry_price) / entry_price * 100
        if drift_pct >= drift_pct_limit:
            logger.info("DRY RUN: price drifted %.2f%% — would cancel order %s", drift_pct, ord_id)
            _cancel_pending(state, f"dry_run_drift_{drift_pct:.2f}pct")
            return state

    logger.info("DRY RUN: pending order %s still within limits", ord_id)
    return state


# ── Helpers ───────────────────────────────────────────────────────────────────

def _do_cancel(inst_id: str, ord_id: str, state: dict[str, Any], reason: str) -> None:
    try:
        with_retry(okx.cancel_order, inst_id, ord_id)
    except OKXError as exc:
        logger.warning("Could not cancel order %s: %s", ord_id, exc)
    _cancel_pending(state, reason)


def _cancel_pending(state: dict[str, Any], reason: str) -> None:
    state["pending_order"] = {
        "active": False,
        "order_id": None,
        "entry_price": None,
        "placed_at": None,
        "side": None,
    }
    state["last_action"] = reason


def _activate_position(
    state: dict[str, Any],
    order: dict,
    partial: bool = False,
) -> None:
    """Move state from pending → in_position after fill."""
    side_str = order.get("side", "buy")
    fill_px = float(order.get("avgPx") or order.get("px", 0))
    fill_sz = float(order.get("accFillSz") or order.get("sz", 0))

    # Preserve TP/SL from the pending order's algo (already set in open_trade)
    existing_pos = state.get("position", {})
    state["position"] = {
        "active": True,
        "side": "long" if side_str == "buy" else "short",
        "entry_price": fill_px,
        "size_contracts": fill_sz,
        "open_time": datetime.now(tz=timezone.utc).isoformat(),
        "sl_price": existing_pos.get("sl_price") or state["pending_order"].get("sl_price"),
        "tp1_price": existing_pos.get("tp1_price") or state["pending_order"].get("tp1_price"),
        "tp2_price": existing_pos.get("tp2_price"),
        "algo_order_id": state["pending_order"].get("algo_id"),
        "entry_order_id": order.get("ordId"),
        "reconciled": False,
        "dry_run": False,
    }
    state["pending_order"] = {
        "active": False,
        "order_id": None,
        "entry_price": None,
        "placed_at": None,
        "side": None,
    }
    state["last_action"] = "position_activated_from_fill" + ("_partial" if partial else "")
    logger.info(
        "Position activated: %s %.4f contracts @ %.2f",
        state["position"]["side"], fill_sz, fill_px,
    )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
