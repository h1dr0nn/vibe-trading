"""btc signals — current technical indicator values in a table."""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, __import__("pathlib").Path(__file__).resolve().parents[1].__str__())

import display
import indicators
import okx_client


def _ema_cross_label(tech) -> tuple[str, str]:
    latest = tech.iloc[-1]
    if latest["ema_12"] > latest["ema_26"]:
        return "12 > 26 ↑", "LONG"
    return "12 < 26 ↓", "SHORT"


def _rsi_label(rsi: float) -> str:
    if rsi > 70:
        return "OVERBOUGHT"
    if rsi < 30:
        return "OVERSOLD"
    if rsi > 55:
        return "BULL"
    if rsi < 45:
        return "BEAR"
    return "NEUTRAL"


def _adx_label(adx: float, di_plus: float, di_minus: float) -> str:
    if adx < 20:
        return "RANGE"
    if adx < 25:
        return "WEAK TREND"
    return "TREND"


def _di_label(di_plus: float, di_minus: float) -> str:
    return "LONG" if di_plus > di_minus else "SHORT"


def _bb_label(bb_pct: float) -> tuple[str, str]:
    if bb_pct > 0.95:
        return f"{bb_pct:.2f} (upper)", "OVERBOUGHT"
    if bb_pct < 0.05:
        return f"{bb_pct:.2f} (lower)", "OVERSOLD"
    if bb_pct > 0.5:
        return f"{bb_pct:.2f}", "BULL"
    return f"{bb_pct:.2f}", "BEAR"


def _obv_label(obv: float, obv_ema20: float) -> str:
    return "BULL" if obv > obv_ema20 else "BEAR"


def _hv_label(hv_pct: float) -> str:
    if hv_pct < 20:
        return "LOW VOL"
    if hv_pct > 80:
        return "HIGH VOL"
    return "NEUTRAL"


def run(args: argparse.Namespace) -> None:
    symbol = args.symbol
    timeframe = args.interval

    try:
        ohlcv = okx_client.get_ohlcv(symbol, bar=timeframe, limit=300)
    except okx_client.OKXAPIError as e:
        display.console.print(f"[red]OKX API error: {e}[/red]")
        sys.exit(1)
    except Exception as e:
        display.console.print(f"[red]Network error: {e}[/red]")
        sys.exit(1)

    if ohlcv.empty or len(ohlcv) < 30:
        display.console.print("[red]Insufficient OHLCV data[/red]")
        sys.exit(1)

    tech = indicators.compute_all(ohlcv)
    latest = tech.iloc[-1]

    ema_cross_val, ema_cross_sig = _ema_cross_label(tech)
    bb_val, bb_sig = _bb_label(latest["bb_pct"])
    rsi_val = latest["rsi"]
    adx_val = latest["adx"]
    di_plus = latest["di_plus"]
    di_minus = latest["di_minus"]

    rows = [
        ("Price",        f"${latest['close']:,.0f}",                          "—"),
        ("EMA(12)",      f"${latest['ema_12']:,.0f}",                         "—"),
        ("EMA(26)",      f"${latest['ema_26']:,.0f}",                         "—"),
        ("EMA Cross",    ema_cross_val,                                        ema_cross_sig),
        ("RSI(14)",      f"{rsi_val:.1f}",                                    _rsi_label(rsi_val)),
        ("ADX(14)",      f"{adx_val:.1f}",                                    _adx_label(adx_val, di_plus, di_minus)),
        ("+DI / -DI",   f"{di_plus:.1f} / {di_minus:.1f}",                  _di_label(di_plus, di_minus)),
        ("BB %B",        bb_val,                                               bb_sig),
        ("BB Width",     f"{latest['bb_width']:.4f}",                         "—"),
        ("OBV vs MA20",  f"{'↑' if latest['obv'] > latest['obv_ema20'] else '↓'}",  _obv_label(latest["obv"], latest["obv_ema20"])),
        ("ATR(14)",      f"${latest['atr']:,.0f}",                            "—"),
        ("HV(20)",       f"{latest['hv20'] * 100:.1f}%",                      "—"),
        ("HV Percentile",f"{latest['hv_pct']:.0f}th",                        _hv_label(latest["hv_pct"])),
    ]

    if args.json:
        data = {r[0]: {"value": r[1], "signal": r[2]} for r in rows}
        display.print_json(data)
    else:
        display.signals_table(rows, symbol, timeframe)
        # Composite quick summary
        bull_count = sum(1 for _, _, s in rows if s in ("LONG", "BULL", "OVERSOLD"))
        bear_count = sum(1 for _, _, s in rows if s in ("SHORT", "BEAR", "OVERBOUGHT"))
        total = bull_count + bear_count
        if total > 0:
            bull_pct = bull_count / total * 100
            summary = "BULLISH" if bull_pct >= 60 else ("BEARISH" if bull_pct <= 40 else "MIXED")
            style = display._bias_style(summary)
            display.console.print(
                f"\n[dim]Composite vote:[/dim] {bull_count} bull / {bear_count} bear  "
                f"→ [{style}]{summary}[/]\n"
            )
