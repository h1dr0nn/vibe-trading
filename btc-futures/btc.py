#!/usr/bin/env python3
"""BTC Futures CLI — practical trading analysis tool.

Usage:
    python btc.py analyze                     Full composite analysis
    python btc.py signals                     Current indicator values
    python btc.py funding                     Funding rate & regime
    python btc.py backtest --strategy <name>  Backtest a strategy
    python btc.py watch                       Live terminal dashboard

Global flags:
    --symbol BTC-USDT     Instrument (default: BTC-USDT)
    --json                Machine-readable JSON output
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Bootstrap: add btc-futures/ to sys.path so commands/* can import siblings
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="btc",
        description="BTC Futures CLI — analysis, signals, funding, backtest, watch",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python btc.py analyze
  python btc.py analyze --symbol ETH-USDT --timeframe 1H
  python btc.py signals --interval 1H
  python btc.py funding
  python btc.py funding --json | python -c "import sys,json; d=json.load(sys.stdin); print(d['regime'])"
  python btc.py backtest --strategy trend-follow
  python btc.py backtest --strategy funding-mean-revert --start 2024-01-01 --leverage 2
  python btc.py watch --refresh 15
        """,
    )
    parser.add_argument("--symbol", default="BTC-USDT", help="Spot symbol (default: BTC-USDT)")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    # ── analyze ──────────────────────────────────────────────────────────────
    p_analyze = sub.add_parser(
        "analyze",
        help="Full composite analysis: technical + funding + liquidation + on-chain",
    )
    p_analyze.add_argument(
        "--timeframe", default="4H",
        choices=["1H", "2H", "4H", "6H", "12H", "1D"],
        help="Technical analysis timeframe (default: 4H)",
    )

    # ── signals ──────────────────────────────────────────────────────────────
    p_signals = sub.add_parser(
        "signals",
        help="Current technical indicator values (EMA, RSI, ADX, BB, OBV, HV)",
    )
    p_signals.add_argument(
        "--interval", default="4H",
        choices=["15m", "1H", "2H", "4H", "6H", "12H", "1D"],
        help="OHLCV timeframe (default: 4H)",
    )

    # ── funding ───────────────────────────────────────────────────────────────
    p_funding = sub.add_parser(
        "funding",
        help="Funding rate, annualised, regime, 7-day history, quarterly basis",
    )
    p_funding.add_argument(
        "--periods", type=int, default=21,
        help="History periods to fetch (default: 21 = 7 days)",
    )
    p_funding.add_argument(
        "--no-basis", action="store_true",
        help="Skip quarterly futures basis calculation",
    )

    # ── trade ─────────────────────────────────────────────────────────────────
    p_trade = sub.add_parser(
        "trade",
        help="Multi-timeframe confluence → LONG/SHORT/NO TRADE with entry, TP, SL",
    )
    p_trade.add_argument(
        "--balance", type=float, default=100.0,
        help="Account balance in USDT for position sizing (default: 100)",
    )
    p_trade.add_argument(
        "--risk-pct", type=float, default=1.0, dest="risk_pct",
        help="Percent of balance to risk per trade (default: 1%%)",
    )
    p_trade.add_argument(
        "--no-agent", action="store_true", dest="no_agent",
        help="Skip vibe-trading agent — use local multi-TF analysis only (fast)",
    )

    # ── backtest ──────────────────────────────────────────────────────────────
    p_bt = sub.add_parser(
        "backtest",
        help="Backtest a named strategy against historical OKX data",
    )
    p_bt.add_argument(
        "--strategy", required=True,
        choices=["funding-mean-revert", "trend-follow", "vol-regime", "oi-trend"],
        metavar="STRATEGY",
        help=(
            "Strategy to run. Choices: "
            "funding-mean-revert | trend-follow | vol-regime | oi-trend"
        ),
    )
    p_bt.add_argument("--start", default=None, help="Start date YYYY-MM-DD (default: 365d ago)")
    p_bt.add_argument("--end",   default=None, help="End date YYYY-MM-DD (default: today)")
    p_bt.add_argument(
        "--interval", default="1D",
        choices=["1H", "4H", "1D"],
        help="Bar size (default: 1D)",
    )
    p_bt.add_argument("--cash",     default=10000, type=float, help="Initial capital USDT (default: 10000)")
    p_bt.add_argument("--leverage", default=1.0,   type=float, help="Leverage (default: 1.0)")
    p_bt.add_argument("--no-trades", action="store_true", help="Skip individual trade table")

    # ── watch ─────────────────────────────────────────────────────────────────
    p_watch = sub.add_parser(
        "watch",
        help="Live dashboard: price, funding, OI, signals — refreshed every N seconds",
    )
    p_watch.add_argument(
        "--refresh", type=int, default=30,
        help="Refresh interval in seconds (default: 30)",
    )
    p_watch.add_argument(
        "--signal-interval", default="4H",
        choices=["15m", "1H", "4H", "1D"],
        dest="signal_interval",
        help="Timeframe for technical signals (default: 4H)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Thread --symbol and --json into args for all subcommands
    # (already present via global parser; subcommand handlers read args.symbol / args.json)

    command = args.command

    try:
        if command == "trade":
            from commands.trade import run
        elif command == "analyze":
            from commands.analyze import run
        elif command == "signals":
            from commands.signals import run
        elif command == "funding":
            from commands.funding import run
        elif command == "backtest":
            from commands.backtest import run
        elif command == "watch":
            from commands.watch import run
        else:
            parser.print_help()
            return 2

        run(args)
        return 0

    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 1


if __name__ == "__main__":
    sys.exit(main())
