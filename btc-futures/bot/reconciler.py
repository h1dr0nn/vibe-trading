"""Startup reconciliation: sync OKX live state vs state.json.

Handles 4 mismatch cases (per BOT.md Section 11.2):
  Case 1 — OKX has position, state.json does not
  Case 2 — state.json thinks position is open, OKX does not
  Case 3 — state.json thinks pending order exists, OKX does not
  Case 4 — OKX has pending order, state.json does not
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from bot import okx_private as okx
from bot.okx_errors import OKXError, PositionNotFoundError, SkipCycleError
from bot.state import save_state

logger = logging.getLogger(__name__)


def reconcile(state: dict[str, Any]) -> dict[str, Any]:
    """Reconcile state.json against OKX live state on startup.

    Mutates state in-place and saves. Returns state.
    Skips reconciliation entirely when DRY_RUN=true.
    """
    if os.getenv("DRY_RUN", "true").lower() == "true":
        logger.info("DRY_RUN mode — skipping startup reconciliation")
        return state

    inst_id = os.getenv("BOT_SYMBOL", "BTC-USDT-SWAP")
    changed = False

    try:
        live_position = okx.get_position(inst_id)
        live_pending = okx.get_pending_orders(inst_id)
        live_algos = okx.get_algo_pending(inst_id)
    except (OKXError, SkipCycleError) as exc:
        logger.error("Reconcile: cannot fetch OKX state — %s. Proceeding with stored state.", exc)
        return state

    # ── Case 1: OKX has position, state.json does not ─────────────────────────
    if live_position and not state["position"]["active"]:
        logger.warning("Reconcile Case 1: OKX has open position not tracked in state.json")
        state["position"] = _build_position_from_live(live_position, live_algos)
        changed = True
        _notify(
            "⚠️ Reconcile: phát hiện position đang mở trên OKX không có trong state. "
            f"Side={state['position']['side']} entry={state['position']['entry_price']:.2f} — đã đồng bộ."
        )

    # ── Case 2: state.json thinks position open, OKX does not ─────────────────
    elif state["position"]["active"] and not live_position:
        logger.warning("Reconcile Case 2: state.json has position but OKX does not — position closed externally")
        old_side = state["position"].get("side", "?")
        _finalize_closed_position(state, reason="reconcile_external_close")
        changed = True
        _notify(
            f"ℹ️ Reconcile: position {old_side} trong state đã đóng trên OKX "
            "(TP/SL hit hoặc đóng tay) — đã cập nhật state."
        )

    # ── Case 3: state.json thinks pending order exists, OKX does not ──────────
    if state["pending_order"]["active"] and not live_pending:
        logger.warning("Reconcile Case 3: state has pending order but OKX does not")
        ord_id = state["pending_order"].get("order_id")
        # Check fill history
        try:
            fills = okx.get_fills(inst_id, limit=10)
            filled_ids = {f.get("ordId") for f in fills}
        except OKXError:
            fills = []
            filled_ids = set()

        if ord_id and ord_id in filled_ids:
            logger.info("Reconcile Case 3a: pending order %s was filled", ord_id)
            fill = next(f for f in fills if f.get("ordId") == ord_id)
            _activate_position_from_fill(state, fill, live_algos)
            changed = True
            _notify(
                f"⚠️ Reconcile: limit order {ord_id} sudah fill saat bot mati — "
                "position diaktifkan dari fill history."
            )
        else:
            logger.info("Reconcile Case 3b: pending order %s was cancelled/expired", ord_id)
            state["pending_order"] = _empty_pending()
            changed = True
            _notify(f"ℹ️ Reconcile: pending order {ord_id} sudah tidak ada di OKX — reset ke IDLE.")

    # ── Case 4: OKX has pending order, state.json does not ────────────────────
    elif not state["pending_order"]["active"] and live_pending and not state["position"]["active"]:
        logger.warning("Reconcile Case 4: OKX has pending order not tracked in state.json")
        order = live_pending[0]
        state["pending_order"] = _build_pending_from_live(order)
        changed = True
        _notify(
            f"⚠️ Reconcile: phát hiện pending order {order.get('ordId')} trên OKX — đã đồng bộ."
        )

    if changed:
        save_state(state)
        logger.info("Reconcile: state saved after changes")
    else:
        logger.info("Reconcile: state is consistent with OKX — no changes needed")

    return state


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_position_from_live(pos: dict, algos: list[dict]) -> dict[str, Any]:
    """Build a position dict from OKX position data."""
    side = "long" if float(pos.get("pos", 0)) > 0 else "short"
    algo_id = algos[0].get("algoId") if algos else None
    tp_price = float(algos[0].get("tpTriggerPx", 0)) if algos else None
    sl_price = float(algos[0].get("slTriggerPx", 0)) if algos else None

    return {
        "active": True,
        "side": side,
        "entry_price": float(pos.get("avgPx", 0)),
        "size_contracts": abs(float(pos.get("pos", 0))),
        "open_time": pos.get("cTime") and _ms_to_iso(int(pos["cTime"])),
        "sl_price": sl_price,
        "tp1_price": tp_price,
        "tp2_price": None,
        "algo_order_id": algo_id,
        "entry_order_id": None,
        "reconciled": True,
        "dry_run": False,
    }


def _finalize_closed_position(state: dict[str, Any], reason: str) -> None:
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
        "dry_run": False,
    }
    state["pending_order"] = _empty_pending()
    state["last_action"] = reason


def _activate_position_from_fill(
    state: dict[str, Any],
    fill: dict,
    algos: list[dict],
) -> None:
    side = "long" if fill.get("side") == "buy" else "short"
    algo_id = algos[0].get("algoId") if algos else None
    state["position"] = {
        "active": True,
        "side": side,
        "entry_price": float(fill.get("fillPx", 0)),
        "size_contracts": abs(float(fill.get("fillSz", 0))),
        "open_time": fill.get("ts") and _ms_to_iso(int(fill["ts"])),
        "sl_price": float(algos[0].get("slTriggerPx", 0)) if algos else None,
        "tp1_price": float(algos[0].get("tpTriggerPx", 0)) if algos else None,
        "tp2_price": None,
        "algo_order_id": algo_id,
        "entry_order_id": fill.get("ordId"),
        "reconciled": True,
        "dry_run": False,
    }
    state["pending_order"] = _empty_pending()


def _build_pending_from_live(order: dict) -> dict[str, Any]:
    return {
        "active": True,
        "order_id": order.get("ordId"),
        "entry_price": float(order.get("px", 0)),
        "placed_at": order.get("cTime") and _ms_to_iso(int(order["cTime"])),
        "side": order.get("side"),
    }


def _empty_pending() -> dict[str, Any]:
    return {
        "active": False,
        "order_id": None,
        "entry_price": None,
        "placed_at": None,
        "side": None,
    }


def _ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _notify(msg: str) -> None:
    """Best-effort Telegram notification — import lazily to avoid circular deps."""
    try:
        from bot.telegram_bot import send_message_sync
        send_message_sync(msg)
    except Exception as exc:
        logger.debug("Reconcile notify failed (Telegram not ready): %s", exc)
