"""Circuit breaker: halt trading when daily loss exceeds threshold."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def is_triggered(state: dict[str, Any]) -> bool:
    """Return True if circuit break is currently active."""
    if not state.get("circuit_break"):
        return False

    # Check if it has expired
    cb_until_str = state.get("circuit_break_until")
    if cb_until_str:
        from bot.state import _parse_dt
        cb_until = _parse_dt(cb_until_str)
        if cb_until and datetime.now(tz=timezone.utc) >= cb_until:
            # Expired — clear it
            state["circuit_break"] = False
            state["circuit_break_until"] = None
            logger.info("Circuit break expired — cleared")
            return False

    return True


def check_and_trigger(state: dict[str, Any], balance: float) -> str:
    """Check daily loss vs threshold. Returns one of:

    - "not_triggered": below threshold
    - "already_active": circuit break already running from earlier
    - "newly_triggered": just crossed threshold this call (caller should notify)
    """
    if is_triggered(state):
        return "already_active"

    max_loss_pct = float(os.getenv("MAX_DAILY_LOSS_PCT", "3.0"))
    daily_loss = state.get("daily_loss_usdt", 0.0)

    if balance <= 0:
        logger.warning("Balance is zero/negative — skipping circuit break check")
        return "not_triggered"

    loss_pct = daily_loss / balance * 100

    if loss_pct >= max_loss_pct:
        _trigger(state, daily_loss, loss_pct, max_loss_pct)
        return "newly_triggered"

    return "not_triggered"


def record_loss(state: dict[str, Any], loss_usdt: float) -> None:
    """Add a realised loss to daily counter. Call after any closed trade."""
    if loss_usdt <= 0:
        return
    state["daily_loss_usdt"] = state.get("daily_loss_usdt", 0.0) + loss_usdt
    logger.info("Daily loss updated: $%.4f total", state["daily_loss_usdt"])


def record_pnl(state: dict[str, Any], pnl_usdt: float) -> None:
    """Record realised PnL (positive or negative) and update daily loss counter."""
    state["daily_realized_pnl"] = state.get("daily_realized_pnl", 0.0) + pnl_usdt
    state["daily_trades"] = state.get("daily_trades", 0) + 1

    if pnl_usdt < 0:
        record_loss(state, abs(pnl_usdt))


def _trigger(
    state: dict[str, Any],
    daily_loss: float,
    loss_pct: float,
    max_loss_pct: float,
) -> None:
    now = datetime.now(tz=timezone.utc)
    # Reset at next UTC midnight
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    state["circuit_break"] = True
    state["circuit_break_until"] = tomorrow.isoformat()
    logger.warning(
        "Circuit break triggered — daily loss $%.4f (%.2f%% >= %.1f%%) until %s",
        daily_loss, loss_pct, max_loss_pct, tomorrow.isoformat(),
    )
