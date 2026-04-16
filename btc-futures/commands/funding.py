"""btc funding — current funding rate, regime, 7-day history, basis."""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, __import__("pathlib").Path(__file__).resolve().parents[1].__str__())

import display
import okx_client


def _classify_regime(rates: list[float]) -> str:
    """Classify funding regime from perp-funding-basis/SKILL.md thresholds."""
    if not rates:
        return "unknown"
    avg = sum(rates) / len(rates)
    last3 = rates[:3]  # newest first from OKX API
    all_pos = all(r > 0 for r in last3)
    all_neg = all(r < 0 for r in last3)

    if avg > 0.0003 and all_pos:
        return "overheated_long"
    if avg > 0.0001 and all_pos:
        return "bullish_carry"
    if avg < -0.0002 and all_neg:
        return "overheated_short"
    if avg < -0.00005 and all_neg:
        return "bearish_carry"
    if abs(avg) < 0.00005:
        return "neutral"
    return "mixed"


def _regime_trading_implication(regime: str) -> str:
    implications = {
        "overheated_long": "Shorts paid by longs — consider mean-revert short or reduce long exposure",
        "bullish_carry":   "Carry trade positive — longs pay premiums, cautious on adding",
        "neutral":         "No clear funding bias — use technical signals for direction",
        "bearish_carry":   "Shorts paying longs — potential short squeeze risk",
        "overheated_short": "Extreme short crowding — watch for short squeeze / counter-rally",
        "mixed":           "Inconsistent funding — no strong structural edge",
        "unknown":         "Insufficient data",
    }
    return implications.get(regime, "—")


def run(args: argparse.Namespace) -> None:
    symbol = args.symbol
    swap_id = symbol.replace("-USDT", "-USDT-SWAP") if not symbol.endswith("-SWAP") else symbol
    spot_id = swap_id.replace("-SWAP", "")

    try:
        current = okx_client.get_funding_rate(swap_id)
        history = okx_client.get_funding_rate_history(swap_id, limit=args.periods)
    except okx_client.OKXAPIError as e:
        display.console.print(f"[red]OKX API error: {e}[/red]")
        sys.exit(1)
    except Exception as e:
        display.console.print(f"[red]Network error: {e}[/red]")
        sys.exit(1)

    import pandas as pd
    rate_8h = float(current.get("fundingRate", 0))
    annualized = rate_8h * 3 * 365 * 100

    hist_rates = [float(h["fundingRate"]) for h in history]
    avg_7d = sum(hist_rates) / len(hist_rates) if hist_rates else 0.0
    regime = _classify_regime(hist_rates)

    next_ts = int(current.get("nextFundingTime", 0))
    next_dt = pd.Timestamp(next_ts, unit="ms", tz="UTC") if next_ts else None
    now_utc = pd.Timestamp.now(tz="UTC")
    minutes_until = int((next_dt - now_utc).total_seconds() / 60) if next_dt else 0

    # Optional basis from nearest quarterly future
    basis = None
    if not args.no_basis:
        try:
            base = spot_id.split("-")[0]
            basis = okx_client.get_nearest_quarterly_basis(base)
        except Exception:
            pass  # basis is optional

    data = {
        "current_8h": rate_8h,
        "annualized_pct": round(annualized, 4),
        "7d_avg_8h": round(avg_7d, 6),
        "regime": regime,
        "regime_implication": _regime_trading_implication(regime),
        "next_funding_utc": str(next_dt) if next_dt else "—",
        "minutes_until_next": minutes_until,
        "basis": basis,
    }

    if args.json:
        display.print_json(data)
    else:
        display.funding_table(data, history)
        implication = _regime_trading_implication(regime)
        display.console.print(f"\n[dim]Implication:[/dim] {implication}\n")
