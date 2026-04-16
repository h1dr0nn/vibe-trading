"""Format all Telegram notification messages per BOT.md Section 13."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def cycle_report(
    state: dict[str, Any],
    balance: float,
    current_price: float,
    funding_rate: float | None = None,
) -> str:
    pos = state.get("position", {})
    now_str = datetime.now(tz=timezone.utc).strftime("%H:%M UTC")
    symbol = "BTC-USDT-SWAP"

    lines = [f"BTC-USDT-SWAP | {now_str}", ""]
    lines.append(f"Balance: ${balance:.2f} USDT")

    if pos.get("active"):
        side = pos.get("side", "?").upper()
        entry = pos.get("entry_price", 0) or 0
        size = pos.get("size_contracts", 0) or 0

        from bot.order_manager import CONTRACT_SIZE
        direction = 1 if pos.get("side") == "long" else -1
        pnl = size * CONTRACT_SIZE * (current_price - entry) * direction
        pnl_pct = pnl / balance * 100 if balance else 0
        pnl_sign = "+" if pnl >= 0 else ""

        emoji = "+" if pnl >= 0 else "-"
        lines.append(
            f"Position: {side} ${entry:,.0f} -> now ${current_price:,.0f} "
            f"({pnl_sign}${pnl:.2f} / {pnl_sign}{pnl_pct:.1f}%)"
        )
        tp1 = pos.get("tp1_price")
        sl = pos.get("sl_price")
        if tp1 and sl:
            lines.append(f"TP1: ${tp1:,.0f} | SL: ${sl:,.0f}")

        # Hours open
        open_time_str = pos.get("open_time")
        if open_time_str:
            try:
                ot = datetime.fromisoformat(open_time_str.replace("Z", "+00:00"))
                if ot.tzinfo is None:
                    ot = ot.replace(tzinfo=timezone.utc)
                hours = (datetime.now(tz=timezone.utc) - ot).total_seconds() / 3600
                lines.append(f"Open: {hours:.1f}h")
            except ValueError:
                pass

    sig = state.get("last_signal")
    if sig:
        direction_label = "LONG" if sig.get("direction") == 1 else ("SHORT" if sig.get("direction") == -1 else "FLAT")
        conf = sig.get("confidence", "?")
        lines.append(f"Signal: {direction_label} {conf}% confidence")

    if funding_rate is not None:
        lines.append(f"Funding: {funding_rate*100:+.4f}%/8h")

    if pos.get("active"):
        lines.append("")
        lines.append("Status: Holding position")
    elif state.get("pending_order", {}).get("active"):
        lines.append("")
        lines.append("Status: Waiting for limit order fill")

    dry = " [DRY RUN]" if state.get("position", {}).get("dry_run") else ""
    if dry:
        lines.append(dry.strip())

    return "\n".join(lines)


def order_placed(
    signal: dict[str, Any],
    contracts: int,
    risk_usdt: float,
    ord_id: str,
    dry_run: bool = False,
) -> str:
    direction = "LONG" if signal.get("direction") == 1 else "SHORT"
    entry = signal.get("entry", 0)
    tp1 = signal.get("tp1", 0)
    tp2 = signal.get("tp2", 0)
    sl = signal.get("sl", 0)
    conf = signal.get("confidence", "?")
    score = signal.get("net_score", 0)
    source = signal.get("source", "local")
    order_type = "limit"

    prefix = "[DRY RUN] " if dry_run else ""
    tp1_pct = abs(tp1 - entry) / entry * 100 if entry else 0
    sl_pct = abs(sl - entry) / entry * 100 if entry else 0
    tp2_pct = abs(tp2 - entry) / entry * 100 if entry else 0

    return (
        f"{prefix}New order placed\n"
        f"\n"
        f"Direction : {direction}\n"
        f"Entry     : ${entry:,.0f} ({order_type})\n"
        f"TP1       : ${tp1:,.0f} (+{tp1_pct:.1f}%)\n"
        f"TP2       : ${tp2:,.0f} (+{tp2_pct:.1f}%)\n"
        f"SL        : ${sl:,.0f} (-{sl_pct:.1f}%)\n"
        f"Size      : {contracts} contracts (risk ${risk_usdt:.2f})\n"
        f"Confidence: {conf}% | Score: {score}\n"
        f"\n"
        f"Source: {source}"
    )


def position_closed(
    side: str,
    entry_price: float,
    close_price: float,
    size_contracts: int | float,
    hold_hours: float,
    balance: float,
    reason: str,
    dry_run: bool = False,
) -> str:
    from bot.order_manager import CONTRACT_SIZE
    direction = 1 if side == "long" else -1
    pnl = size_contracts * CONTRACT_SIZE * (close_price - entry_price) * direction
    pnl_pct = pnl / (balance - pnl) * 100 if (balance - pnl) != 0 else 0
    sign = "+" if pnl >= 0 else ""
    emoji = "Win" if pnl >= 0 else "Loss"
    prefix = "[DRY RUN] " if dry_run else ""
    reason_label = _reason_label(reason)

    h = int(hold_hours)
    m = int((hold_hours - h) * 60)

    return (
        f"{prefix}Position closed — {reason_label}\n"
        f"\n"
        f"Entry  : ${entry_price:,.0f}\n"
        f"Close  : ${close_price:,.0f}\n"
        f"PnL    : {sign}${pnl:.2f} ({sign}{pnl_pct:.2f}%)\n"
        f"Hold   : {h}h {m}m\n"
        f"Balance: ${balance:.2f} USDT"
    )


def no_trade(reason: str, signal: dict[str, Any], next_run_str: str) -> str:
    direction = "LONG" if signal.get("direction") == 1 else ("SHORT" if signal.get("direction") == -1 else "FLAT")
    conf = signal.get("confidence", "?")
    score = signal.get("net_score", 0)

    return (
        f"Analysis complete — No Trade\n"
        f"\n"
        f"Reason: {reason}\n"
        f"Signal: {direction} {conf}% (score {score})\n"
        f"\n"
        f"Next cycle: {next_run_str}"
    )


def circuit_break_alert(daily_loss: float, loss_pct: float, max_pct: float, until_str: str) -> str:
    return (
        f"CIRCUIT BREAKER triggered\n"
        f"\n"
        f"Daily loss: ${daily_loss:.2f} ({loss_pct:.1f}% of balance)\n"
        f"Limit     : {max_pct:.1f}%\n"
        f"\n"
        f"Bot paused until {until_str} UTC.\n"
        f"Type /status to check."
    )


def danger_alert(reasons: list[str], position: dict[str, Any], close_price: float) -> str:
    side = position.get("side", "?").upper()
    entry = position.get("entry_price", 0) or 0
    sl = position.get("sl_price", 0) or 0
    pct = abs(close_price - entry) / entry * 100 if entry else 0
    sign = "+" if close_price >= entry else "-"

    reason_text = "\n".join(f"  - {r}" for r in reasons)
    return (
        f"DANGER — closing position\n"
        f"\n"
        f"Reasons:\n{reason_text}\n"
        f"\n"
        f"Side   : {side}\n"
        f"Entry  : ${entry:,.0f}\n"
        f"Current: ${close_price:,.0f} ({sign}{pct:.1f}%)\n"
        f"SL     : ${sl:,.0f}\n"
        f"\n"
        f"Executing market close..."
    )


def status_report(state: dict[str, Any], balance: float, current_price: float) -> str:
    pos = state.get("position", {})
    cb = state.get("circuit_break", False)
    paused = state.get("bot_paused", False)

    lines = ["Bot Status\n"]
    lines.append(f"Balance : ${balance:.2f} USDT")

    if cb:
        lines.append(f"Circuit : TRIGGERED (until {state.get('circuit_break_until', '?')})")
    elif paused:
        lines.append("Status  : PAUSED")
    else:
        lines.append("Status  : RUNNING")

    if pos.get("active"):
        side = pos.get("side", "?").upper()
        entry = pos.get("entry_price", 0) or 0
        size = pos.get("size_contracts", 0) or 0
        from bot.order_manager import CONTRACT_SIZE
        direction = 1 if pos.get("side") == "long" else -1
        pnl = size * CONTRACT_SIZE * (current_price - entry) * direction
        pnl_sign = "+" if pnl >= 0 else ""
        lines.append(f"Position: {side} {size} contracts @ ${entry:,.0f}")
        lines.append(f"PnL     : {pnl_sign}${pnl:.2f}")
        lines.append(f"Current : ${current_price:,.0f}")
    else:
        lines.append("Position: NONE")

    daily_pnl = state.get("daily_realized_pnl", 0)
    daily_trades = state.get("daily_trades", 0)
    pnl_sign = "+" if daily_pnl >= 0 else ""
    lines.append(f"Daily   : {pnl_sign}${daily_pnl:.2f} ({daily_trades} trades)")

    last_action = state.get("last_action")
    if last_action:
        lines.append(f"Last act: {last_action}")

    return "\n".join(lines)


def close_confirm_prompt(position: dict[str, Any], current_price: float) -> str:
    side = position.get("side", "?").upper()
    size = position.get("size_contracts", 0)
    entry = position.get("entry_price", 0) or 0
    from bot.order_manager import CONTRACT_SIZE
    direction = 1 if position.get("side") == "long" else -1
    pnl = (size or 0) * CONTRACT_SIZE * (current_price - entry) * direction
    pnl_sign = "+" if pnl >= 0 else ""

    return (
        f"Confirm close position?\n"
        f"\n"
        f"Side   : {side}\n"
        f"Size   : {size} contracts\n"
        f"Entry  : ${entry:,.0f}\n"
        f"Current: ${current_price:,.0f} ({pnl_sign}${pnl:.2f})\n"
        f"\n"
        f"Type /close confirm to execute.\n"
        f"Expires in 60 seconds."
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _reason_label(reason: str) -> str:
    mapping = {
        "danger_close_to_sl": "Price near SL",
        "danger_signal_flip": "Signal flip",
        "danger_funding": "Extreme funding",
        "danger_too_long": "Held too long",
        "danger_floating_loss": "Floating loss limit",
        "manual_close": "Manual (/close command)",
        "reconcile_external_close": "Externally closed",
    }
    for key, label in mapping.items():
        if key in reason:
            return label
    return reason.replace("_", " ").title()
