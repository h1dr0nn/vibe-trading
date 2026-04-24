"""Telegram message formatting. Uses HTML + emojis for readability.

All send_message_sync() calls use parse_mode="HTML", so <b>, <i>, <code>, <pre>
tags are rendered. Keep messages compact — mobile screens are narrow.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


# ── Emoji constants ───────────────────────────────────────────────────────────

E_LONG = "🟢"
E_SHORT = "🔴"
E_FLAT = "⚪"
E_WIN = "✅"
E_LOSS = "❌"
E_WARN = "⚠️"
E_DANGER = "🚨"
E_CHART = "📊"
E_MONEY = "💰"
E_CLOCK = "⏱"
E_ROCKET = "🚀"
E_SHIELD = "🛡"
E_PAUSE = "⏸"
E_PLAY = "▶️"
E_TARGET = "🎯"
E_STOP = "🛑"
E_DICE = "🎲"
E_BELL = "🔔"
E_GEAR = "⚙️"
E_INFO = "ℹ️"


def _side_emoji(side: str) -> str:
    return E_LONG if side.lower() in ("long", "buy") else E_SHORT if side.lower() in ("short", "sell") else E_FLAT


def _dir_emoji(direction: int) -> str:
    return E_LONG if direction == 1 else E_SHORT if direction == -1 else E_FLAT


def _dir_label(direction: int) -> str:
    return "LONG" if direction == 1 else "SHORT" if direction == -1 else "FLAT"


def _pnl_emoji(pnl: float) -> str:
    return E_WIN if pnl > 0 else E_LOSS if pnl < 0 else "➖"


def _fmt_usdt(amount: float) -> str:
    sign = "+" if amount > 0 else ("" if amount == 0 else "")
    return f"{sign}${amount:,.2f}"


def _fmt_pct(value: float) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"


def _fmt_price(price: float) -> str:
    return f"${price:,.0f}"


# ── Messages ──────────────────────────────────────────────────────────────────

def cycle_report(
    state: dict[str, Any],
    balance: float,
    current_price: float,
    funding_rate: float | None = None,
) -> str:
    pos = state.get("position", {})
    now_str = datetime.now(tz=timezone.utc).strftime("%H:%M UTC")
    symbol = "BTC-USDT-SWAP"

    lines = [f"{E_CHART} <b>{symbol}</b> · <i>{now_str}</i>"]
    lines.append(f"{E_MONEY} Balance: <b>${balance:,.2f}</b>")

    if pos.get("active"):
        side = pos.get("side", "?")
        side_label = side.upper()
        entry = pos.get("entry_price", 0) or 0
        size = pos.get("size_contracts", 0) or 0

        from bot.order_manager import CONTRACT_SIZE
        direction = 1 if side == "long" else -1
        pnl = size * CONTRACT_SIZE * (current_price - entry) * direction
        pnl_pct = pnl / balance * 100 if balance else 0

        lines.append("")
        lines.append(f"{_side_emoji(side)} <b>{side_label}</b> · {size} contracts")
        lines.append(f"   Entry: <code>{_fmt_price(entry)}</code>  Now: <code>{_fmt_price(current_price)}</code>")
        lines.append(f"   PnL: <b>{_fmt_usdt(pnl)}</b> ({_fmt_pct(pnl_pct)}) {_pnl_emoji(pnl)}")

        tp1 = pos.get("tp1_price")
        sl = pos.get("sl_price")
        if tp1 and sl:
            lines.append(f"   {E_TARGET} TP1: <code>{_fmt_price(tp1)}</code>  {E_STOP} SL: <code>{_fmt_price(sl)}</code>")

        open_time_str = pos.get("open_time")
        if open_time_str:
            try:
                ot = datetime.fromisoformat(open_time_str.replace("Z", "+00:00"))
                if ot.tzinfo is None:
                    ot = ot.replace(tzinfo=timezone.utc)
                hours = (datetime.now(tz=timezone.utc) - ot).total_seconds() / 3600
                lines.append(f"   {E_CLOCK} Open: {hours:.1f}h")
            except ValueError:
                pass

    sig = state.get("last_signal")
    if sig:
        direction = sig.get("direction", 0)
        conf = sig.get("confidence", "?")
        score = sig.get("net_score", 0)
        lines.append("")
        lines.append(f"{E_DICE} Signal: {_dir_emoji(direction)} <b>{_dir_label(direction)}</b> · conf <b>{conf}%</b> · score <code>{score:+.2f}</code>")

    if funding_rate is not None:
        lines.append(f"{E_INFO} Funding: <code>{funding_rate*100:+.4f}%</code> /8h")

    if pos.get("active"):
        lines.append(f"\n{E_SHIELD} <i>Holding position</i>")
    elif state.get("pending_order", {}).get("active"):
        lines.append(f"\n{E_CLOCK} <i>Waiting for limit order fill</i>")

    if state.get("position", {}).get("dry_run"):
        lines.append(f"\n{E_WARN} <i>DRY RUN mode</i>")

    return "\n".join(lines)


def _tf_breakdown_lines(signal: dict[str, Any]) -> list[str]:
    """Render per-TF direction/score as a compact block."""
    tfb = signal.get("tf_breakdown") or {}
    if not tfb:
        return []
    # Match main.py TIMEFRAMES ordering
    order = ["15m", "1H", "4H", "1D"]
    direction = signal.get("direction", 0)
    agreeing = signal.get("agreeing_tfs", 0)

    lines = [f"{E_CHART} <b>TF breakdown</b>"]
    for tf in order:
        d = tfb.get(tf)
        if not d:
            continue
        tf_sig = d.get("signal", 0)
        tf_score = d.get("score", 0)
        rsi = d.get("rsi", 0)
        adx = d.get("adx", 0)
        ema = d.get("ema_cross", "")
        mark = E_WIN if (tf_sig == direction and direction != 0) else E_LOSS if tf_sig == -direction else "➖"
        lines.append(
            f"   {mark} {tf:<4} {_dir_emoji(tf_sig)} score <code>{tf_score:+.2f}</code> · "
            f"RSI {rsi:.0f} · ADX {adx:.0f} · EMA <i>{ema}</i>"
        )
    # 15m excluded from agreement count — note which TFs count
    lines.append(f"   <i>Agreement: {agreeing}/3 big TFs (1H·4H·1D)</i>")
    return lines


def order_placed(
    signal: dict[str, Any],
    contracts: int,
    risk_usdt: float,
    ord_id: str,
    dry_run: bool = False,
) -> str:
    direction = signal.get("direction", 0)
    side_emoji = _dir_emoji(direction)
    side_label = _dir_label(direction)
    entry = signal.get("entry", 0)
    tp1 = signal.get("tp1", 0)
    tp2 = signal.get("tp2", 0)
    sl = signal.get("sl", 0)
    conf = signal.get("confidence", "?")
    score = signal.get("net_score", 0)
    source = signal.get("source", "local")

    tp1_pct = abs(tp1 - entry) / entry * 100 if entry else 0
    tp2_pct = abs(tp2 - entry) / entry * 100 if entry else 0
    sl_pct = abs(sl - entry) / entry * 100 if entry else 0

    header = f"{E_WARN} [DRY RUN] " if dry_run else f"{E_ROCKET} "
    reasoning = (signal.get("reasoning") or "").strip()

    lines = [
        f"{header}<b>New order placed</b>",
        "",
        f"{side_emoji} <b>{side_label}</b> · limit · {contracts} contracts",
        f"{E_CHART} Entry: <code>{_fmt_price(entry)}</code>",
        f"{E_TARGET} TP1: <code>{_fmt_price(tp1)}</code> ({_fmt_pct(tp1_pct)})",
        f"{E_TARGET} TP2: <code>{_fmt_price(tp2)}</code> ({_fmt_pct(tp2_pct)})",
        f"{E_STOP} SL : <code>{_fmt_price(sl)}</code> (-{sl_pct:.2f}%)",
        "",
        f"{E_DICE} Confidence: <b>{conf}%</b> · Score <code>{score:+.2f}</code>",
        f"{E_MONEY} Risk: <b>${risk_usdt:.2f}</b>",
        f"{E_INFO} Source: <i>{source}</i>",
    ]
    if reasoning:
        lines.append(f"💡 <b>Reasoning</b>: <i>{_truncate(reasoning, 380)}</i>")
    tf_lines = _tf_breakdown_lines(signal)
    if tf_lines:
        lines.append("")
        lines.extend(tf_lines)
    return "\n".join(lines)


def _truncate(text: str, limit: int) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


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
    denom = balance - pnl
    pnl_pct = pnl / denom * 100 if denom else 0

    prefix = f"{E_WARN} [DRY RUN] " if dry_run else ""
    header_emoji = E_WIN if pnl >= 0 else E_LOSS
    reason_label = _reason_label(reason)

    h = int(hold_hours)
    m = int((hold_hours - h) * 60)

    return "\n".join([
        f"{prefix}{header_emoji} <b>Position closed</b> · <i>{reason_label}</i>",
        "",
        f"{_side_emoji(side)} {side.upper()}",
        f"   Entry: <code>{_fmt_price(entry_price)}</code>",
        f"   Close: <code>{_fmt_price(close_price)}</code>",
        f"   PnL  : <b>{_fmt_usdt(pnl)}</b> ({_fmt_pct(pnl_pct)})",
        "",
        f"{E_CLOCK} Hold: {h}h {m}m",
        f"{E_MONEY} Balance: <b>${balance:,.2f}</b>",
    ])


def no_trade(reason: str, signal: dict[str, Any], next_run_str: str) -> str:
    direction = signal.get("direction", 0)
    conf = signal.get("confidence", "?")
    score = signal.get("net_score", 0)

    lines = [
        f"{E_PAUSE} <b>No trade</b>",
        "",
        f"Reason: <i>{reason}</i>",
        f"Signal: {_dir_emoji(direction)} <b>{_dir_label(direction)}</b> · conf <b>{conf}%</b> · score <code>{score:+.2f}</code>",
    ]
    reasoning = (signal.get("reasoning") or "").strip()
    if reasoning:
        lines.append(f"💡 <i>{_truncate(reasoning, 320)}</i>")
    tf_lines = _tf_breakdown_lines(signal)
    if tf_lines:
        lines.append("")
        lines.extend(tf_lines)
    lines.append("")
    lines.append(f"{E_CLOCK} Next cycle: <i>{next_run_str}</i>")
    return "\n".join(lines)


def circuit_break_alert(daily_loss: float, loss_pct: float, max_pct: float, until_str: str) -> str:
    return "\n".join([
        f"{E_DANGER} <b>CIRCUIT BREAKER triggered</b>",
        "",
        f"{E_LOSS} Daily loss: <b>${daily_loss:,.2f}</b> ({loss_pct:.1f}% of balance)",
        f"{E_SHIELD} Limit: {max_pct:.1f}%",
        "",
        f"{E_PAUSE} Bot paused until <b>{until_str}</b> UTC",
        f"{E_INFO} Type /status to check",
    ])


def trail_moved(position: dict[str, Any], old_sl: float, new_sl: float, reason: str, current_price: float) -> str:
    side = position.get("side", "?")
    entry = position.get("entry_price", 0) or 0
    size = position.get("size_contracts", 0) or 0
    tp1 = position.get("tp1_price", 0) or 0
    from bot.order_manager import CONTRACT_SIZE
    direction = 1 if side == "long" else -1
    pnl = size * CONTRACT_SIZE * (current_price - entry) * direction
    locked = size * CONTRACT_SIZE * (new_sl - entry) * direction

    return "\n".join([
        f"{E_SHIELD} <b>SL moved</b> · <i>{reason}</i>",
        "",
        f"{_side_emoji(side)} <b>{side.upper()}</b> · {size} contracts",
        f"   Entry : <code>{_fmt_price(entry)}</code>",
        f"   Now   : <code>{_fmt_price(current_price)}</code>  PnL: <b>{_fmt_usdt(pnl)}</b> {_pnl_emoji(pnl)}",
        f"   SL    : <code>{_fmt_price(old_sl)}</code> → <code>{_fmt_price(new_sl)}</code>",
        f"   {E_TARGET} TP1: <code>{_fmt_price(tp1)}</code>",
        "",
        f"{E_INFO} Locked: <b>{_fmt_usdt(locked)}</b> if SL hits",
    ])


def danger_alert(reasons: list[str], position: dict[str, Any], close_price: float) -> str:
    side = position.get("side", "?")
    side_label = side.upper()
    entry = position.get("entry_price", 0) or 0
    sl = position.get("sl_price", 0) or 0
    pct = (close_price - entry) / entry * 100 if entry else 0

    reason_text = "\n".join(f"  • <i>{r}</i>" for r in reasons)
    return "\n".join([
        f"{E_DANGER} <b>DANGER — closing position</b>",
        "",
        f"Reasons:",
        reason_text,
        "",
        f"{_side_emoji(side)} <b>{side_label}</b>",
        f"   Entry  : <code>{_fmt_price(entry)}</code>",
        f"   Current: <code>{_fmt_price(close_price)}</code> ({_fmt_pct(pct)})",
        f"   SL     : <code>{_fmt_price(sl)}</code>",
        "",
        f"{E_STOP} Executing market close…",
    ])


def status_report(state: dict[str, Any], balance: float, current_price: float) -> str:
    pos = state.get("position", {})
    cb = state.get("circuit_break", False)
    paused = state.get("bot_paused", False)

    if cb:
        status_line = f"{E_DANGER} CIRCUIT BREAK <i>(until {state.get('circuit_break_until', '?')})</i>"
    elif paused:
        status_line = f"{E_PAUSE} <b>PAUSED</b>"
    else:
        status_line = f"{E_PLAY} <b>RUNNING</b>"

    lines = [
        f"{E_GEAR} <b>Bot Status</b>",
        "",
        f"Status : {status_line}",
        f"{E_MONEY} Balance: <b>${balance:,.2f}</b>",
    ]

    if pos.get("active"):
        side = pos.get("side", "?")
        entry = pos.get("entry_price", 0) or 0
        size = pos.get("size_contracts", 0) or 0
        from bot.order_manager import CONTRACT_SIZE
        direction = 1 if side == "long" else -1
        pnl = size * CONTRACT_SIZE * (current_price - entry) * direction
        pnl_pct = pnl / balance * 100 if balance else 0
        lines.append("")
        lines.append(f"{_side_emoji(side)} <b>{side.upper()}</b> · {size} contracts @ <code>{_fmt_price(entry)}</code>")
        lines.append(f"   Now: <code>{_fmt_price(current_price)}</code>  PnL: <b>{_fmt_usdt(pnl)}</b> ({_fmt_pct(pnl_pct)}) {_pnl_emoji(pnl)}")
    else:
        lines.append(f"{E_FLAT} Position: <i>none</i>")
        lines.append(f"   Current: <code>{_fmt_price(current_price)}</code>")

    daily_pnl = state.get("daily_realized_pnl", 0)
    daily_trades = state.get("daily_trades", 0)
    lines.append("")
    lines.append(f"{E_CHART} Daily: <b>{_fmt_usdt(daily_pnl)}</b> · {daily_trades} trades {_pnl_emoji(daily_pnl)}")

    last_action = state.get("last_action")
    if last_action:
        lines.append(f"{E_INFO} Last: <i>{last_action}</i>")

    return "\n".join(lines)


def close_confirm_prompt(position: dict[str, Any], current_price: float) -> str:
    side = position.get("side", "?")
    size = position.get("size_contracts", 0)
    entry = position.get("entry_price", 0) or 0
    from bot.order_manager import CONTRACT_SIZE
    direction = 1 if side == "long" else -1
    pnl = (size or 0) * CONTRACT_SIZE * (current_price - entry) * direction

    return "\n".join([
        f"{E_WARN} <b>Confirm close position?</b>",
        "",
        f"{_side_emoji(side)} <b>{side.upper()}</b> · {size} contracts",
        f"   Entry  : <code>{_fmt_price(entry)}</code>",
        f"   Current: <code>{_fmt_price(current_price)}</code>",
        f"   PnL    : <b>{_fmt_usdt(pnl)}</b> {_pnl_emoji(pnl)}",
        "",
        f"{E_BELL} Send <code>/close confirm</code> to execute",
        f"{E_CLOCK} Expires in 60 seconds",
    ])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _reason_label(reason: str) -> str:
    mapping = {
        "danger_close_to_sl": "Price near SL",
        "danger_signal_flip": "Signal flip",
        "danger_funding": "Extreme funding",
        "danger_too_long": "Held too long",
        "danger_floating_loss": "Floating loss limit",
        "manual_close": "Manual close",
        "reconcile_external_close": "Externally closed",
        "tp1_hit": "TP1 hit",
        "tp2_hit": "TP2 hit",
        "sl_hit": "SL hit",
    }
    for key, label in mapping.items():
        if key in reason:
            return label
    return reason.replace("_", " ").title()
