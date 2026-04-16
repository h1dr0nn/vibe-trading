"""Rich terminal output and JSON serialization helpers."""

from __future__ import annotations

import json
import sys
from typing import Any

from rich import box
from rich.console import Console
from rich.panel import Panel

# Force UTF-8 output on Windows to avoid cp1252 encode errors
if sys.platform == "win32":
    import io
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
from rich.table import Table
from rich.text import Text

console = Console(legacy_windows=False)

# ── Signal color helpers ──────────────────────────────────────────────────────

def _signal_style(label: str) -> str:
    label = label.upper()
    if label in ("LONG", "BULL", "BUY", "BULLISH", "ACCUMULATE"):
        return "bold green"
    if label in ("SHORT", "BEAR", "SELL", "BEARISH", "DISTRIBUTE"):
        return "bold red"
    if label in ("NEUTRAL", "RANGE", "FLAT", "HOLD", "—"):
        return "yellow"
    return "white"


def _bias_style(bias: str) -> str:
    b = bias.upper()
    if "LONG" in b or "BULL" in b:
        return "bold green"
    if "SHORT" in b or "BEAR" in b:
        return "bold red"
    return "bold yellow"


def _pct_style(value: float, good_positive: bool = True) -> str:
    if good_positive:
        return "green" if value > 0 else ("red" if value < 0 else "white")
    return "red" if value > 0 else ("green" if value < 0 else "white")


# ── JSON output ───────────────────────────────────────────────────────────────

def _to_serializable(obj: Any) -> Any:
    if isinstance(obj, float):
        return round(obj, 6)
    if isinstance(obj, dict):
        return {k: _to_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_serializable(i) for i in obj]
    return obj


def print_json(data: Any) -> None:
    print(json.dumps(_to_serializable(data), indent=2, ensure_ascii=False))


# ── Funding table ─────────────────────────────────────────────────────────────

def funding_table(data: dict, history: list[dict]) -> None:
    regime = data.get("regime", "—")
    rate_8h = data.get("current_8h", 0)
    ann = data.get("annualized_pct", 0)
    avg_7d = data.get("7d_avg_8h", 0)
    next_funding = data.get("next_funding_utc", "—")
    minutes = data.get("minutes_until_next", 0)
    basis = data.get("basis")

    # Main info table
    t = Table(box=box.ROUNDED, show_header=False, padding=(0, 1))
    t.add_column("Key", style="dim", width=22)
    t.add_column("Value", min_width=30)

    ann_style = _pct_style(ann, good_positive=False)  # high annualized = bearish for longs
    t.add_row("Funding Rate (8h)", f"[{'green' if rate_8h < 0 else 'red'}]{rate_8h:+.6f}[/]")
    t.add_row("Annualised", f"[{ann_style}]{ann:+.2f}%[/]")
    t.add_row("7-Day Average (8h)", f"{avg_7d:+.6f}")
    t.add_row("Regime", f"[{_bias_style(regime)}]{regime}[/]")
    t.add_row("Next Settlement", f"{next_funding}  ({minutes}m away)")

    if basis:
        basis_style = "green" if basis.get("annualized_basis_pct", 0) > 0 else "red"
        t.add_row("Nearest Future", basis.get("inst_id", "—"))
        t.add_row("Basis (spot→future)", f"[{basis_style}]{basis.get('basis_pct', 0):+.4f}%  ({basis.get('annualized_basis_pct', 0):+.1f}% ann)[/]")
        t.add_row("Days to Expiry", str(basis.get("days_to_expiry", "—")))

    console.print(Panel(t, title="[bold cyan]BTC Funding & Basis[/]", border_style="cyan"))

    # History sparkline
    if history:
        rates = [float(h.get("fundingRate", 0)) for h in history]
        _print_funding_history(rates)


def _print_funding_history(rates: list[float]) -> None:
    t = Table(box=box.SIMPLE, title="7-Day Funding History (newest → oldest)", title_style="dim")
    t.add_column("Period", style="dim", width=4)
    t.add_column("Rate (8h)", width=12)
    t.add_column("Annualised", width=12)
    t.add_column("Bar", width=20)

    max_abs = max(abs(r) for r in rates) if rates else 1e-8
    for i, r in enumerate(rates):
        ann = r * 3 * 365 * 100
        bar_len = int(abs(r) / max(max_abs, 1e-8) * 15)
        bar_char = "█" * bar_len
        style = "green" if r <= 0 else "red"
        t.add_row(
            f"T-{i}",
            f"[{style}]{r:+.6f}[/]",
            f"[{style}]{ann:+.1f}%[/]",
            f"[{style}]{bar_char}[/]",
        )

    console.print(t)


# ── Signals table ─────────────────────────────────────────────────────────────

def signals_table(rows: list[tuple[str, str, str]], symbol: str, timeframe: str) -> None:
    t = Table(box=box.ROUNDED, title=f"[bold]{symbol} Signals ({timeframe})[/]", title_style="cyan")
    t.add_column("Indicator", style="dim", min_width=18)
    t.add_column("Value", min_width=14)
    t.add_column("Signal", min_width=10)

    for name, value, signal in rows:
        t.add_row(name, value, f"[{_signal_style(signal)}]{signal}[/]")

    console.print(t)


# ── Analyze panel ─────────────────────────────────────────────────────────────

def analyze_panel(
    composite: dict,
    tech: dict,
    funding: dict,
    liquidation: dict,
    onchain: dict,
) -> None:
    bias = composite.get("bias", "neutral").upper()
    score = composite.get("score", 0.0)
    confidence = composite.get("confidence", "low").upper()

    bias_style = _bias_style(bias)
    score_bar = _score_bar(score)

    # Header panel
    header = Text()
    header.append(f"  BIAS: ", style="bold white")
    header.append(f"{bias}", style=bias_style)
    header.append(f"  │  Score: {score:+.2f}  │  Confidence: {confidence}", style="dim")
    header.append(f"\n  {score_bar}", style="white")
    console.print(Panel(header, border_style=bias_style.split()[-1], title="[bold]BTC Futures Analysis[/]"))

    # Components table
    t = Table(box=box.SIMPLE, show_header=True)
    t.add_column("Component", style="dim", width=20)
    t.add_column("Signal", width=10)
    t.add_column("Detail", min_width=40)

    # Technical
    tech_sig = _dir_label(tech.get("signal", 0))
    t.add_row(
        "Technical (4H)",
        f"[{_signal_style(tech_sig)}]{tech_sig}[/]",
        f"EMA {'↑' if tech.get('ema_cross') == 'bullish' else '↓'}  "
        f"RSI={tech.get('rsi', 0):.0f}  ADX={tech.get('adx', 0):.0f}  "
        f"BB={tech.get('bb_position', '—')}"
    )

    # Funding
    f_sig = _dir_label(funding.get("signal", 0))
    f_regime = funding.get("regime", "—")
    f_ann = funding.get("annualized", 0)
    t.add_row(
        "Funding Regime",
        f"[{_signal_style(f_sig)}]{f_sig}[/]",
        f"{f_regime}  |  {f_ann:+.1f}% ann  |  7d-avg: {funding.get('avg_7d', 0):+.5f}"
    )

    # Liquidation
    liq_bias = liquidation.get("bias", "balanced")
    liq_risk = liquidation.get("cascade_risk", "low")
    liq_sig = "LONG" if "upward" in liq_bias else ("SHORT" if "downward" in liq_bias else "NEUTRAL")
    t.add_row(
        "Liq. Levels",
        f"[{_signal_style(liq_sig)}]{liq_sig}[/]",
        f"Magnet: {liq_bias}  |  Cascade risk: {liq_risk}"
    )

    # On-chain proxy
    oc_score = onchain.get("score", 3)
    oc_sig = "LONG" if oc_score >= 4 else ("SHORT" if oc_score <= 2 else "NEUTRAL")
    t.add_row(
        "On-chain Proxy",
        f"[{_signal_style(oc_sig)}]{oc_sig}[/]",
        f"Score: {oc_score}/5  |  OI Δ: {onchain.get('oi_change_pct', 0):+.2f}%  "
        f"|  Vol/Price div: {onchain.get('vol_price_div', '—')}"
    )

    console.print(t)


def _dir_label(signal: int | float) -> str:
    if signal > 0:
        return "LONG"
    if signal < 0:
        return "SHORT"
    return "NEUTRAL"


def _score_bar(score: float) -> str:
    """Visual score bar from -1 to +1."""
    width = 30
    mid = width // 2
    pos = int((score + 1) / 2 * width)
    bar = ["-"] * width
    bar[mid] = "│"
    if 0 <= pos < width:
        bar[pos] = "█"
    return f"[-1] {''.join(bar)} [+1]  ({score:+.2f})"


# ── Metrics table (backtest) ──────────────────────────────────────────────────

def metrics_table(metrics: dict, strategy: str, start: str, end: str, initial_cash: float) -> None:
    t = Table(box=box.ROUNDED, title=f"[bold cyan]Backtest: {strategy}[/]  {start} → {end}")
    t.add_column("Metric", style="dim", min_width=22)
    t.add_column("Value", min_width=14)

    def _fmt_pct(key: str) -> str:
        v = metrics.get(key)
        if v is None:
            return "—"
        pct = v * 100
        style = "green" if pct > 0 else ("red" if pct < 0 else "white")
        return f"[{style}]{pct:.2f}%[/]"

    def _fmt_float(key: str, decimals: int = 3, good_positive: bool = True) -> str:
        v = metrics.get(key)
        if v is None:
            return "—"
        style = _pct_style(float(v), good_positive)
        return f"[{style}]{float(v):.{decimals}f}[/]"

    total_ret = metrics.get("total_return", 0)
    ret_style = "green" if total_ret > 0 else "red"

    t.add_row("Total Return", f"[{ret_style}]{total_ret * 100:.2f}%[/]")
    t.add_row("Annualised Return", _fmt_pct("annualized_return"))
    t.add_row("Sharpe Ratio", _fmt_float("sharpe", 3))
    t.add_row("Sortino Ratio", _fmt_float("sortino", 3))
    t.add_row("Max Drawdown", _fmt_float("max_drawdown", 3, good_positive=False))
    t.add_row("Win Rate", f"{metrics.get('win_rate', 0) * 100:.1f}%")
    t.add_row("Profit Factor", _fmt_float("profit_factor", 3))
    t.add_row("Initial Cash", f"${initial_cash:,.0f}")

    console.print(t)


# ── Watch layout helpers ──────────────────────────────────────────────────────

def watch_row(snap: dict) -> None:
    """Print a single-line watch update (used inside rich.live)."""
    from rich.columns import Columns

    price = snap.get("price", 0)
    change_24h = snap.get("change_24h_pct", 0)
    mark = snap.get("mark_price", 0)
    funding = snap.get("funding_8h", 0)
    funding_ann = snap.get("funding_ann_pct", 0)
    regime = snap.get("regime", "—")
    oi_usd = snap.get("oi_usd", 0)
    oi_change = snap.get("oi_change_pct", 0)

    price_style = "green" if change_24h >= 0 else "red"
    funding_style = "red" if funding > 0 else "green"
    oi_style = "green" if oi_change > 0 else "red"

    panels = [
        Panel(
            f"[{price_style}]${price:,.0f}[/]\n[dim]{change_24h:+.2f}% 24h[/]",
            title="Price", border_style=price_style.split()[-1]
        ),
        Panel(
            f"Mark [dim]${mark:,.0f}[/]",
            title="Mark", border_style="dim"
        ),
        Panel(
            f"[{funding_style}]{funding:+.6f}[/]\n[dim]{funding_ann:+.1f}% ann | {regime}[/]",
            title="Funding (8h)", border_style=funding_style
        ),
        Panel(
            f"[{oi_style}]${oi_usd / 1e9:.2f}B[/]\n[dim]{oi_change:+.2f}% Δ[/]",
            title="Open Interest", border_style=oi_style
        ),
    ]

    console.print(Columns(panels, equal=True))
