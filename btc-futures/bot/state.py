"""Atomic load/save of state.json and daily reset logic."""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_STATE: dict[str, Any] = {
    "version": 1,
    "last_run": None,
    "next_run": None,

    "position": {
        "active": False,
        "side": None,           # "long" | "short"
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
    },

    "pending_order": {
        "active": False,
        "order_id": None,
        "entry_price": None,
        "placed_at": None,
        "side": None,
    },

    "circuit_break": False,
    "circuit_break_until": None,

    "daily_loss_usdt": 0.0,
    "daily_realized_pnl": 0.0,
    "daily_trades": 0,
    "daily_reset_date": None,

    "last_action": None,
    "last_signal": None,

    "bot_paused": False,
    "pending_close_confirm": False,
}


def _state_path() -> Path:
    base = os.getenv("BOT_STATE_PATH", "state.json")
    return Path(base)


def load_state() -> dict[str, Any]:
    """Load state.json, returning default state if file missing or corrupt."""
    path = _state_path()

    if not path.exists():
        logger.info("state.json not found — initialising fresh state")
        state = _deep_copy(_DEFAULT_STATE)
        state["daily_reset_date"] = _today_utc()
        return state

    try:
        with open(path) as f:
            loaded = json.load(f)
        # Merge with defaults so new keys are always present
        state = _deep_copy(_DEFAULT_STATE)
        _deep_merge(state, loaded)
        return state
    except (json.JSONDecodeError, OSError) as exc:
        # Try backup
        bak = Path(str(path) + ".bak")
        if bak.exists():
            logger.warning("state.json corrupt (%s) — loading backup", exc)
            try:
                with open(bak) as f:
                    loaded = json.load(f)
                state = _deep_copy(_DEFAULT_STATE)
                _deep_merge(state, loaded)
                return state
            except Exception:
                pass
        logger.error("state.json and backup both unreadable — starting fresh")
        state = _deep_copy(_DEFAULT_STATE)
        state["daily_reset_date"] = _today_utc()
        return state


def save_state(state: dict[str, Any]) -> None:
    """Atomic write: backup → tmp → rename."""
    path = _state_path()
    bak = Path(str(path) + ".bak")
    tmp = Path(str(path) + ".tmp")

    # Backup existing
    if path.exists():
        shutil.copy2(path, bak)

    # Write to temp
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)

    # Atomic rename
    os.replace(tmp, path)


def reset_daily_if_needed(state: dict[str, Any]) -> dict[str, Any]:
    """Reset daily counters at UTC 00:00. Returns (possibly mutated) state."""
    today = _today_utc()
    if state.get("daily_reset_date") != today:
        state["daily_loss_usdt"] = 0.0
        state["daily_realized_pnl"] = 0.0
        state["daily_trades"] = 0
        state["daily_reset_date"] = today
        # Also reset circuit break if it expired
        cb_until = state.get("circuit_break_until")
        if cb_until and _parse_dt(cb_until) <= datetime.now(tz=timezone.utc):
            state["circuit_break"] = False
            state["circuit_break_until"] = None
            logger.info("Circuit break cleared on daily reset")
        logger.info("Daily counters reset for %s", today)
    return state


# ── Helpers ───────────────────────────────────────────────────────────────────

def _today_utc() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _deep_copy(obj: Any) -> Any:
    return json.loads(json.dumps(obj))


def _deep_merge(base: dict, override: dict) -> None:
    """Merge override into base in-place, recursively for nested dicts."""
    for key, val in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val
