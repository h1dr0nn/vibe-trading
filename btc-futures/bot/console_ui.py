"""Rich console dashboard for the bot runtime.

Renders compact, colored panels and tables directly to stdout. Coexists
with file-based logging — the logger writes a full ISO trail to bot.log
for grep, while this module shows a curated, pretty view on the console.

Usage: import the helpers and call them at key moments (startup, cycle
header, signal analysis, trade events). Do not route logging through
here — that would spam the dashboard.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from rich import box
from rich.align import Align
from rich.columns import Columns
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

_console = Console(highlight=False, log_path=False)


# ── Palette ───────────────────────────────────────────────────────────────────
C_LONG   = "green"
C_SHORT  = "red"
C_WARN   = "yellow"
C_DANGER = "bright_red"
C_OK     = "bright_green"
C_INFO   = "cyan"
C_MUTED  = "grey50"
C_ACCENT = "bright_magenta"
C_GOLD   = "gold1"


def _now_hhmmss() -> str:
    return datetime.now(tz=timezone.utc).strftime("%H:%M:%S")


def _side_color(side: str | None) -> str:
    if not side:
        return C_MUTED
    return C_LONG if side.lower() in ("long", "buy") else C_SHORT


def _dir_style(direction: int) -> tuple[str, str]:
    if direction == 1:
        return "🟢 LONG", C_LONG
    if direction == -1:
        return "🔴 SHORT", C_SHORT
    return "⚪ FLAT", C_MUTED


# ── Startup banner ────────────────────────────────────────────────────────────

def startup_banner(cfg: dict[str, Any]) -> None:
    """One-time banner when bot boots. cfg keys: symbol, mode, interval,
    leverage, risk_pct, agent, regime_filter."""
    mode = cfg.get("mode", "?")
    mode_color = {
        "LIVE": C_OK, "DEMO": C_GOLD, "DRY RUN": C_WARN,
    }.get(mode, C_INFO)

    grid = Table.grid(padding=(0, 2), expand=False)
    grid.add_column(style=C_MUTED, justify="right")
    grid.add_column(style="bold")
    grid.add_row("Mode", Text(mode, style=f"bold {mode_color}"))
    grid.add_row("Symbol", cfg.get("symbol", "?"))
    grid.add_row("Cycle", f"every {cfg.get('interval', '?')}h")
    grid.add_row("Risk", f"{cfg.get('risk_pct', '?')}% · Leverage {cfg.get('leverage', '?')}x")
    grid.add_row("Agent", "ON" if cfg.get("agent") else "OFF")
    grid.add_row("Regime filter", "ON" if cfg.get("regime_filter") else "OFF")
    grid.add_row("Started", datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))

    title = Text("🚀 BTC Futures Bot", style=f"bold {C_ACCENT}")
    _console.print()
    _console.print(Panel(
        Align.center(grid),
        title=title,
        border_style=mode_color,
        box=box.DOUBLE_EDGE,
        padding=(1, 2),
        width=60,
    ))
    _console.print()


# ── Cycle header ──────────────────────────────────────────────────────────────

def cycle_header(state_label: str, price: float, balance: float) -> None:
    """One-line rule shown at the top of each full cycle."""
    bits = [
        Text(f"[{_now_hhmmss()}]", style=C_MUTED),
        Text(" · "),
        Text(state_label, style=f"bold {C_ACCENT}"),
        Text(" · "),
        Text(f"${price:,.0f}", style=C_GOLD),
        Text(" · "),
        Text(f"Bal ${balance:,.2f}", style=C_INFO),
    ]
    _console.print()
    _console.print(Rule(Text.assemble(*bits), style=C_MUTED))


# ── Signal analysis table ─────────────────────────────────────────────────────

def signal_table(
    tf_breakdown: dict[str, dict[str, Any]],
    confluence: dict[str, Any],
    confidence_pct: int,
) -> None:
    table = Table(
        box=box.SIMPLE_HEAVY,
        show_header=True,
        header_style=f"bold {C_INFO}",
        padding=(0, 1),
        expand=False,
    )
    table.add_column("TF", style="bold", justify="left")
    table.add_column("Signal", justify="left")
    table.add_column("Score", justify="right")
    table.add_column("RSI", justify="right")
    table.add_column("ADX", justify="right")
    table.add_column("EMA", justify="left")

    for tf in ("15m", "1H", "4H", "1D"):
        row = tf_breakdown.get(tf) or {}
        sig = row.get("signal", 0)
        label, color = _dir_style(sig)
        score = row.get("score", 0) or 0
        score_style = C_LONG if score > 0 else C_SHORT if score < 0 else C_MUTED
        adx = row.get("adx", 0) or 0
        adx_style = C_OK if adx >= 25 else C_WARN if adx >= 18 else C_MUTED
        ema_label = row.get("ema_cross", "-")
        ema_style = C_LONG if ema_label == "bull" else C_SHORT if ema_label == "bear" else C_MUTED

        table.add_row(
            tf,
            Text(label, style=color),
            Text(f"{score:+.2f}", style=score_style),
            f"{row.get('rsi', 0):.0f}",
            Text(f"{adx:.0f}", style=adx_style),
            Text(ema_label, style=ema_style),
        )

    # Footer
    agreeing = confluence.get("agreeing_tfs", 0)
    net = confluence.get("net_score", 0) or 0
    conf_label = confluence.get("confidence", "?")
    conf_color = {"HIGH": C_OK, "MEDIUM": C_WARN, "LOW": C_DANGER}.get(conf_label, C_MUTED)

    footer = Text.assemble(
        ("Confluence ", C_MUTED),
        (conf_label, f"bold {conf_color}"),
        (" · ", C_MUTED),
        (f"{agreeing}/3 big TFs agree", C_INFO),
        (" · ", C_MUTED),
        (f"net {net:+.2f}", C_ACCENT),
        (" · ", C_MUTED),
        (f"conf {confidence_pct}%", C_GOLD),
    )

    _console.print(Panel(
        Group(table, Text(""), footer),
        title=Text("📊 Multi-TF Confluence", style=f"bold {C_INFO}"),
        border_style=C_INFO,
        box=box.ROUNDED,
        padding=(0, 1),
    ))


# ── Event panels ──────────────────────────────────────────────────────────────

def no_trade_panel(reason: str, signal: dict[str, Any] | None, next_run: str) -> None:
    direction = (signal or {}).get("direction", 0)
    label, color = _dir_style(direction) if direction else ("⏸ No signal", C_MUTED)
    conf = (signal or {}).get("confidence", 0) or 0

    body = Text.assemble(
        ("Reason: ", C_MUTED),
        (reason, "white"),
        ("\nSignal: ", C_MUTED),
        (label, color),
        ("  conf ", C_MUTED),
        (f"{conf}%", C_GOLD),
        ("\nNext  : ", C_MUTED),
        (next_run, C_INFO),
    )
    _console.print(Panel(
        body,
        title=Text("⏸  No Trade", style=f"bold {C_WARN}"),
        border_style=C_WARN,
        box=box.ROUNDED,
        padding=(0, 1),
    ))


def order_placed_panel(
    signal: dict[str, Any],
    contracts: int,
    risk_usdt: float,
    order_id: str,
    dry_run: bool,
) -> None:
    direction = signal.get("direction", 0)
    label, color = _dir_style(direction)
    entry = float(signal.get("entry", 0) or 0)
    sl    = float(signal.get("sl", 0) or 0)
    tp1   = float(signal.get("tp1", 0) or 0)
    tp2   = float(signal.get("tp2", 0) or 0)
    conf  = signal.get("confidence", 0) or 0
    net   = signal.get("net_score", 0) or 0
    src   = signal.get("source", "local")

    sl_pct  = (sl - entry) / entry * 100 if entry else 0
    tp1_pct = (tp1 - entry) / entry * 100 if entry else 0
    tp2_pct = (tp2 - entry) / entry * 100 if entry else 0

    grid = Table.grid(padding=(0, 2), expand=False)
    grid.add_column(style=C_MUTED, justify="right")
    grid.add_column()
    grid.add_row("Side", Text.assemble((label, color), ("  · ", C_MUTED), (f"{contracts} contract{'s' if contracts!=1 else ''}", "bold")))
    grid.add_row("Entry", Text(f"${entry:,.2f}", style=C_GOLD))
    grid.add_row("TP1",   Text(f"${tp1:,.2f}  ({tp1_pct:+.2f}%)", style=C_OK))
    grid.add_row("TP2",   Text(f"${tp2:,.2f}  ({tp2_pct:+.2f}%)", style=C_OK))
    grid.add_row("SL",    Text(f"${sl:,.2f}  ({sl_pct:+.2f}%)", style=C_DANGER))
    grid.add_row("Conf",  Text.assemble((f"{conf}%", C_GOLD), ("  · net ", C_MUTED), (f"{net:+.2f}", C_ACCENT)))
    grid.add_row("Risk",  Text(f"${risk_usdt:.2f}", style=C_INFO))
    grid.add_row("Source", Text(src, style=C_MUTED))
    if dry_run:
        grid.add_row("Mode", Text("DRY RUN", style=f"bold {C_WARN}"))

    title_icon = "🚀" if not dry_run else "🧪"
    _console.print(Panel(
        grid,
        title=Text(f"{title_icon} Order Placed · ordId {order_id}", style=f"bold {color}"),
        border_style=color,
        box=box.ROUNDED,
        padding=(0, 1),
    ))


def position_closed_panel(
    side: str,
    entry: float,
    close_price: float,
    pnl_usdt: float,
    hold_hours: float,
    balance: float,
    reason: str,
) -> None:
    color = _side_color(side)
    pnl_color = C_OK if pnl_usdt > 0 else C_DANGER if pnl_usdt < 0 else C_MUTED
    pnl_pct = (close_price - entry) / entry * 100 * (1 if side == "long" else -1) if entry else 0

    grid = Table.grid(padding=(0, 2), expand=False)
    grid.add_column(style=C_MUTED, justify="right")
    grid.add_column()
    grid.add_row("Side", Text((side or "?").upper(), style=f"bold {color}"))
    grid.add_row("Entry", Text(f"${entry:,.2f}", style=C_GOLD))
    grid.add_row("Close", Text(f"${close_price:,.2f}", style=C_GOLD))
    grid.add_row("PnL",   Text(f"${pnl_usdt:+,.2f}  ({pnl_pct:+.2f}%)", style=f"bold {pnl_color}"))
    grid.add_row("Hold",  Text(f"{int(hold_hours)}h {int((hold_hours % 1) * 60)}m", style=C_INFO))
    grid.add_row("Balance", Text(f"${balance:,.2f}", style=C_INFO))
    grid.add_row("Reason", Text(reason, style=C_MUTED))

    icon = "✅" if pnl_usdt > 0 else "❌" if pnl_usdt < 0 else "➖"
    _console.print(Panel(
        grid,
        title=Text(f"{icon} Position Closed", style=f"bold {pnl_color}"),
        border_style=pnl_color,
        box=box.ROUNDED,
        padding=(0, 1),
    ))


def danger_panel(reasons: list[str], position: dict[str, Any], price: float) -> None:
    side = position.get("side") or "?"
    entry = float(position.get("entry_price") or 0)
    sl = float(position.get("sl_price") or 0)
    pnl_pct = (price - entry) / entry * 100 * (1 if side == "long" else -1) if entry else 0

    body_lines: list[Text] = []
    for r in reasons:
        body_lines.append(Text.assemble(("  • ", C_DANGER), (r, "white")))

    header = Text.assemble(
        (f"{side.upper()} ", f"bold {_side_color(side)}"),
        (f"entry ${entry:,.0f}  ", C_MUTED),
        (f"now ${price:,.0f} ", C_GOLD),
        (f"({pnl_pct:+.2f}%)  ", C_DANGER if pnl_pct < 0 else C_OK),
        (f"SL ${sl:,.0f}", C_MUTED),
    )
    _console.print(Panel(
        Group(header, Text(""), *body_lines),
        title=Text("🚨 DANGER — Closing Position", style=f"bold {C_DANGER}"),
        border_style=C_DANGER,
        box=box.HEAVY,
        padding=(0, 1),
    ))


def cycle_report_panel(
    position: dict[str, Any],
    balance: float,
    price: float,
    pnl_usdt: float,
    pnl_pct: float,
    hold_hours: float,
) -> None:
    """Shown each full cycle while holding a position (not on 5-min ticks)."""
    side = position.get("side") or "?"
    color = _side_color(side)
    pnl_color = C_OK if pnl_usdt > 0 else C_DANGER if pnl_usdt < 0 else C_MUTED
    entry = float(position.get("entry_price") or 0)
    tp1 = float(position.get("tp1_price") or 0)
    sl = float(position.get("sl_price") or 0)

    grid = Table.grid(padding=(0, 2), expand=False)
    grid.add_column(style=C_MUTED, justify="right")
    grid.add_column()
    grid.add_row("Position", Text(f"{side.upper()} · {position.get('size_contracts', 0)} contract(s)", style=f"bold {color}"))
    grid.add_row("Entry",    Text(f"${entry:,.2f}", style=C_GOLD))
    grid.add_row("Now",      Text(f"${price:,.2f}", style=C_GOLD))
    grid.add_row("PnL",      Text(f"${pnl_usdt:+,.2f}  ({pnl_pct:+.2f}%)", style=f"bold {pnl_color}"))
    grid.add_row("TP1 / SL", Text.assemble(
        (f"${tp1:,.0f}", C_OK), (" / ", C_MUTED), (f"${sl:,.0f}", C_DANGER),
    ))
    grid.add_row("Hold",     Text(f"{hold_hours:.1f}h", style=C_INFO))
    grid.add_row("Balance",  Text(f"${balance:,.2f}", style=C_INFO))

    _console.print(Panel(
        grid,
        title=Text("🛡  Holding Position", style=f"bold {C_INFO}"),
        border_style=color,
        box=box.ROUNDED,
        padding=(0, 1),
    ))
