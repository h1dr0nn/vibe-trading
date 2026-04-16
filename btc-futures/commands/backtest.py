"""btc backtest — run a named strategy against historical BTC data."""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

# Bootstrap agent/ onto sys.path for backtest.* imports
_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENT_DIR = _REPO_ROOT / "agent"
for _p in [str(Path(__file__).resolve().parents[1]), str(_AGENT_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import display
import strategies as strat_module
from strategies import STRATEGY_REGISTRY


def run(args: argparse.Namespace) -> None:
    strategy_name = args.strategy

    if strategy_name not in STRATEGY_REGISTRY:
        display.console.print(
            f"[red]Unknown strategy '{strategy_name}'.[/red]\n"
            f"Available: {', '.join(STRATEGY_REGISTRY.keys())}"
        )
        sys.exit(1)

    start = args.start or str(date.today() - timedelta(days=365))
    end = args.end or str(date.today())
    interval = args.interval
    initial_cash = float(args.cash)
    leverage = float(args.leverage)
    symbol = args.symbol

    display.console.print(
        f"[dim]Running backtest: [bold]{strategy_name}[/bold] | "
        f"{symbol} | {interval} | {start} → {end} | cash=${initial_cash:,.0f} | lev={leverage}x[/dim]"
    )

    # Import backtest infrastructure
    try:
        from backtest.engines.crypto import CryptoEngine
        from backtest.engines.base import _align
        from backtest.metrics import calc_metrics, calc_bars_per_year
    except ImportError as e:
        display.console.print(
            f"[red]Cannot import backtest engine: {e}[/red]\n"
            f"[dim]Ensure agent/ dir exists at: {_AGENT_DIR}[/dim]"
        )
        sys.exit(1)

    # Fetch OHLCV via existing OKX loader
    try:
        from backtest.loaders.okx import DataLoader as OKXLoader
        loader = OKXLoader()
        display.console.print(f"[dim]Fetching OHLCV from OKX...[/dim]")
        data_map = loader.fetch(
            codes=[symbol],
            start_date=start,
            end_date=end,
            interval=interval,
            fields=None,
        )
    except Exception as e:
        display.console.print(f"[red]Data fetch failed: {e}[/red]")
        sys.exit(1)

    if not data_map:
        display.console.print(f"[red]No data returned for {symbol} ({start} → {end})[/red]")
        sys.exit(1)

    # Generate signals
    signal_engine = STRATEGY_REGISTRY[strategy_name]()
    display.console.print(f"[dim]Generating signals...[/dim]")
    try:
        signal_map = signal_engine.generate(data_map)
    except Exception as e:
        display.console.print(f"[red]Signal generation failed: {e}[/red]")
        sys.exit(1)

    valid_codes = [c for c in signal_map if c in data_map]
    if not valid_codes:
        display.console.print("[red]No valid signals generated.[/red]")
        sys.exit(1)

    # Align signals (shift by 1 bar — next-bar-open semantics)
    try:
        dates, close_df, target_pos, ret_df = _align(data_map, signal_map, valid_codes)
    except Exception as e:
        display.console.print(f"[red]Signal alignment failed: {e}[/red]")
        sys.exit(1)

    # Run engine
    config = {
        "initial_cash": initial_cash,
        "leverage": leverage,
        "maker_rate": 0.0002,
        "taker_rate": 0.0005,
        "slippage": 0.0005,
        "funding_rate": 0.0001,
    }
    engine = CryptoEngine(config)

    display.console.print(f"[dim]Executing {len(dates)} bars...[/dim]")
    try:
        engine._execute_bars(dates, data_map, close_df, target_pos, valid_codes)
    except Exception as e:
        display.console.print(f"[red]Backtest execution failed: {e}[/red]")
        sys.exit(1)

    if not engine.equity_snapshots:
        display.console.print("[red]No equity snapshots — strategy produced no trades.[/red]")
        sys.exit(1)

    import pandas as pd
    equity_series = pd.Series(
        [s.equity for s in engine.equity_snapshots],
        index=[s.timestamp for s in engine.equity_snapshots],
    )

    bars_per_year = calc_bars_per_year(interval, "okx")
    metrics = calc_metrics(equity_series, engine.trades, initial_cash, bars_per_year)

    # Trade summary
    total_trades = len(engine.trades)
    winning = sum(1 for t in engine.trades if t.pnl > 0)
    losing = sum(1 for t in engine.trades if t.pnl <= 0)
    liq_count = sum(1 for t in engine.trades if getattr(t, "exit_reason", "") == "liquidation")

    output = {
        "strategy": strategy_name,
        "symbol": symbol,
        "interval": interval,
        "start": start,
        "end": end,
        "initial_cash": initial_cash,
        "leverage": leverage,
        "total_return": metrics.get("total_return"),
        "annualized_return": metrics.get("annualized_return"),
        "sharpe": metrics.get("sharpe"),
        "sortino": metrics.get("sortino"),
        "max_drawdown": metrics.get("max_drawdown"),
        "win_rate": metrics.get("win_rate"),
        "profit_factor": metrics.get("profit_factor"),
        "total_trades": total_trades,
        "winning_trades": winning,
        "losing_trades": losing,
        "liquidations": liq_count,
    }

    if args.json:
        display.print_json(output)
    else:
        display.metrics_table(metrics, strategy_name, start, end, initial_cash)
        display.console.print(
            f"\n[dim]Trades:[/dim] {total_trades} total  "
            f"[green]{winning} win[/green] / [red]{losing} loss[/red]"
            + (f"  [bold red]{liq_count} liquidations[/bold red]" if liq_count else "")
        )

        # Show top 5 trades by PnL
        if engine.trades and not args.no_trades:
            sorted_trades = sorted(engine.trades, key=lambda t: abs(t.pnl), reverse=True)[:5]
            t_table = display.Table(box=display.box.SIMPLE, title="Top 5 Trades by |PnL|")
            t_table.add_column("Symbol", style="dim")
            t_table.add_column("Direction")
            t_table.add_column("PnL", min_width=12)
            t_table.add_column("Entry")
            t_table.add_column("Exit")
            for tr in sorted_trades:
                direction = "LONG" if tr.direction == 1 else "SHORT"
                pnl_style = "green" if tr.pnl > 0 else "red"
                t_table.add_row(
                    tr.symbol,
                    direction,
                    f"[{pnl_style}]{tr.pnl:+.2f}[/]",
                    str(tr.entry_time)[:10] if hasattr(tr, "entry_time") else "—",
                    str(tr.exit_time)[:10] if hasattr(tr, "exit_time") else "—",
                )
            display.console.print(t_table)
        display.console.print()
