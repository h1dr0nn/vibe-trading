"""btc watch — live terminal dashboard: price, funding, OI, signals."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, __import__("pathlib").Path(__file__).resolve().parents[1].__str__())

import indicators
import okx_client
from commands.funding import _classify_regime


def _build_layout(snap: dict) -> object:
    """Build a Rich renderable from the current snapshot."""
    from rich import box
    from rich.columns import Columns
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    price = snap.get("price", 0)
    change_24h = snap.get("change_24h_pct", 0)
    mark = snap.get("mark_price", 0)
    index = snap.get("index_price", 0)
    funding_8h = snap.get("funding_8h", 0)
    funding_ann = snap.get("funding_ann_pct", 0)
    regime = snap.get("regime", "—")
    minutes_next = snap.get("minutes_to_settlement", 0)
    oi_usd = snap.get("oi_usd", 0)
    timestamp = snap.get("timestamp", "—")

    price_style = "bold green" if change_24h >= 0 else "bold red"
    arrow = "▲" if change_24h >= 0 else "▼"
    funding_style = "red" if funding_8h > 0 else "green"
    oi_style = "green" if snap.get("oi_change_pct", 0) >= 0 else "red"

    # Price panel
    price_panel = Panel(
        Text.from_markup(
            f"[{price_style}]${price:,.0f}[/]\n"
            f"[dim]{arrow} {abs(change_24h):.2f}% 24h[/]\n"
            f"[dim]Mark  ${mark:,.0f}[/]\n"
            f"[dim]Index ${index:,.0f}[/]"
        ),
        title="[bold]BTC Price[/]",
        border_style="green" if change_24h >= 0 else "red",
    )

    # Funding panel
    funding_panel = Panel(
        Text.from_markup(
            f"[{funding_style}]{funding_8h:+.6f}[/]\n"
            f"[dim]{funding_ann:+.2f}% ann[/]\n"
            f"[dim]{regime}[/]\n"
            f"[dim]Next in {minutes_next}m[/]"
        ),
        title="[bold]Funding (8h)[/]",
        border_style=funding_style,
    )

    # OI panel
    oi_panel = Panel(
        Text.from_markup(
            f"[{oi_style}]${oi_usd / 1e9:.2f}B[/]\n"
            f"[dim]{snap.get('oi_contracts', 0):,.0f} contracts[/]"
        ),
        title="[bold]Open Interest[/]",
        border_style=oi_style,
    )

    # Signals panel (if available)
    sig = snap.get("signals", {})
    if sig:
        sig_lines = []
        for name, val in sig.items():
            sig_lines.append(f"[dim]{name:<14}[/] {val}")
        signals_panel = Panel(
            Text.from_markup("\n".join(sig_lines)),
            title="[bold]Signals[/]",
            border_style="dim",
        )
        top = Columns([price_panel, funding_panel, oi_panel, signals_panel], equal=True)
    else:
        top = Columns([price_panel, funding_panel, oi_panel], equal=True)

    from rich.console import Group
    from rich.text import Text as RText
    footer = RText(f"  Updated: {timestamp}  |  Ctrl+C to exit", style="dim")
    return Group(top, footer)


def _fetch_snapshot(symbol: str, swap_id: str, ohlcv_cache: dict) -> dict:
    """Fetch current market data. Uses cached OHLCV unless refresh_ohlcv=True."""
    import pandas as pd

    snap: dict = {}
    try:
        ticker = okx_client.get_ticker(symbol)
        snap["price"] = float(ticker.get("last", 0))
        open_24h = float(ticker.get("open24h", snap["price"]))
        snap["change_24h_pct"] = (snap["price"] - open_24h) / open_24h * 100 if open_24h else 0
    except Exception:
        snap["price"] = 0
        snap["change_24h_pct"] = 0

    try:
        mark = okx_client.get_mark_price(swap_id)
        snap["mark_price"] = float(mark.get("markPx", snap["price"]))
    except Exception:
        snap["mark_price"] = snap["price"]

    try:
        index = okx_client.get_index_ticker(symbol.replace("-USDT", "-USD"))
        snap["index_price"] = float(index.get("idxPx", snap["price"]))
    except Exception:
        snap["index_price"] = snap["price"]

    try:
        funding = okx_client.get_funding_rate(swap_id)
        rate_8h = float(funding.get("fundingRate", 0))
        snap["funding_8h"] = rate_8h
        snap["funding_ann_pct"] = rate_8h * 3 * 365 * 100
        next_ts = int(funding.get("nextFundingTime", 0))
        if next_ts:
            next_dt = pd.Timestamp(next_ts, unit="ms", tz="UTC")
            now_utc = pd.Timestamp.now(tz="UTC")
            snap["minutes_to_settlement"] = max(int((next_dt - now_utc).total_seconds() / 60), 0)
        else:
            snap["minutes_to_settlement"] = 0
    except Exception:
        snap["funding_8h"] = 0
        snap["funding_ann_pct"] = 0
        snap["minutes_to_settlement"] = 0

    try:
        hist = okx_client.get_funding_rate_history(swap_id, limit=9)
        rates = [float(h["fundingRate"]) for h in hist]
        snap["regime"] = _classify_regime(rates)
    except Exception:
        snap["regime"] = "—"

    try:
        oi = okx_client.get_open_interest(swap_id)
        snap["oi_usd"] = float(oi.get("oiUsd", 0))
        snap["oi_contracts"] = float(oi.get("oi", 0))
        snap["oi_change_pct"] = 0  # would need previous reading for delta
    except Exception:
        snap["oi_usd"] = 0
        snap["oi_contracts"] = 0
        snap["oi_change_pct"] = 0

    # Signals from cached OHLCV
    if ohlcv_cache.get("data") is not None:
        try:
            tech = indicators.compute_all(ohlcv_cache["data"])
            latest = tech.iloc[-1]
            rsi_val = latest["rsi"]
            snap["signals"] = {
                "RSI(14)":  f"{rsi_val:.1f}",
                "EMA Cross": "↑ Bull" if latest["ema_12"] > latest["ema_26"] else "↓ Bear",
                "ADX(14)":  f"{latest['adx']:.1f}",
                "BB %B":    f"{latest['bb_pct']:.2f}",
                "OBV":      "↑" if latest["obv"] > latest["obv_ema20"] else "↓",
            }
        except Exception:
            snap["signals"] = {}
    else:
        snap["signals"] = {}

    snap["timestamp"] = datetime.now(tz=timezone.utc).strftime("%H:%M:%S UTC")
    return snap


def run(args: argparse.Namespace) -> None:
    symbol = args.symbol
    swap_id = symbol.replace("-USDT", "-USDT-SWAP") if not symbol.endswith("-SWAP") else symbol
    refresh = args.refresh
    ohlcv_refresh_every = 10  # ticks

    from rich.live import Live
    from rich.console import Console
    con = Console()

    # Pre-fetch OHLCV for signals
    ohlcv_cache: dict = {"data": None, "tick": 0}
    con.print(f"[dim]Loading OHLCV for {symbol} ({args.signal_interval})...[/dim]")
    try:
        ohlcv_cache["data"] = okx_client.get_ohlcv(symbol, bar=args.signal_interval, limit=300)
    except Exception as e:
        con.print(f"[yellow]OHLCV fetch failed, signals unavailable: {e}[/yellow]")

    tick = 0
    with Live(console=con, refresh_per_second=1) as live:
        while True:
            try:
                snap = _fetch_snapshot(symbol, swap_id, ohlcv_cache)
                live.update(_build_layout(snap))

                tick += 1
                ohlcv_cache["tick"] = tick

                # Refresh OHLCV every N ticks
                if tick % ohlcv_refresh_every == 0:
                    try:
                        ohlcv_cache["data"] = okx_client.get_ohlcv(
                            symbol, bar=args.signal_interval, limit=300
                        )
                    except Exception:
                        pass  # keep stale cache

                time.sleep(refresh)

            except KeyboardInterrupt:
                break
            except Exception as e:
                from rich.panel import Panel
                live.update(Panel(f"[red]Error: {e}[/red]\nRetrying in {refresh}s...", border_style="red"))
                time.sleep(refresh)

    con.print("\n[dim]Watch stopped.[/dim]")
