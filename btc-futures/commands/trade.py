"""btc trade — multi-timeframe confluence + vibe-trading agent → LONG/SHORT + entry/TP/SL.

Flow:
  Phase 1 (always, ~1-2s): Parallel OKX data fetch → local multi-TF scoring
  Phase 2 (if agent ready): Call vibe-trading agent with embedded snapshot → deep analysis
  Phase 3: Parse agent output → display signal with entry/TP/SL
  Fallback: if agent not configured or fails → local multi-TF result
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, __import__("pathlib").Path(__file__).resolve().parents[1].__str__())

import display
import indicators
import okx_client
from commands.funding import _classify_regime
from commands.trade_agent import (
    TradeSignal,
    build_market_snapshot,
    build_prompt,
    check_agent_configured,
    local_fallback_signal,
    parse_agent_output,
    run_agent_subprocess,
)

TIMEFRAMES = ["15m", "1H", "4H", "1D"]

SL_ATR_MULT  = 1.5
TP1_ATR_MULT = 2.0
TP2_ATR_MULT = 3.5


# ── Technical scoring ─────────────────────────────────────────────────────────

def _score_tf(df) -> dict:
    tech = indicators.compute_all(df)
    l = tech.iloc[-1]
    votes = []

    ema_dir = 1 if l["ema_12"] > l["ema_26"] else -1
    votes.append(("ema_cross", ema_dir, 2.0))

    if l["adx"] > 25:
        di_dir = 1 if l["di_plus"] > l["di_minus"] else -1
        votes.append(("adx_di", di_dir, 1.5))

    rsi = l["rsi"]
    if rsi > 55:
        votes.append(("rsi", 1, 1.0))
    elif rsi < 45:
        votes.append(("rsi", -1, 1.0))

    ema50_dir = 1 if l["close"] > l["ema_50"] else -1
    votes.append(("ema50", ema50_dir, 1.0))

    obv_dir = 1 if l["obv"] > l["obv_ema20"] else -1
    votes.append(("obv", obv_dir, 0.5))

    if l["bb_pct"] > 0.6:
        votes.append(("bb", 1, 0.5))
    elif l["bb_pct"] < 0.4:
        votes.append(("bb", -1, 0.5))

    total_w = sum(w for _, _, w in votes)
    norm = sum(d * w for _, d, w in votes) / total_w
    signal = 1 if norm > 0.1 else (-1 if norm < -0.1 else 0)

    bb_pct = float(l["bb_pct"])
    try:
        import math
        bb_pct = 0.5 if math.isnan(bb_pct) else bb_pct
    except Exception:
        pass

    return {
        "signal": signal,
        "strength": int(abs(norm) * 100),
        "score": round(norm, 3),
        "close": float(l["close"]),
        "atr": float(l["atr"]),
        "rsi": round(float(rsi), 1),
        "adx": round(float(l["adx"]), 1),
        "ema_cross": "bull" if ema_dir == 1 else "bear",
        "bb_pct": round(bb_pct, 3),
        "votes": {name: d for name, d, _ in votes},
    }


def _confluence(tf_scores: dict) -> dict:
    # 15m has low weight — entry refinement only, not direction driver
    weights = {"15m": 0.5, "1H": 1.0, "4H": 2.0, "1D": 1.5}
    total_w = sum(weights.values())
    net = sum(tf_scores[tf]["score"] * weights[tf] for tf in TIMEFRAMES) / total_w

    # Counter-trend block: based on 1D vs 4H only (15m/1H too noisy for this check)
    s1d = tf_scores["1D"]["score"]
    s4h = tf_scores["4H"]["score"]
    counter = (s1d > 0.3 and s4h < -0.3) or (s1d < -0.3 and s4h > 0.3)
    signal = 1 if net > 0.15 else (-1 if net < -0.15 else 0)
    if counter:
        signal = 0

    # Count agreeing TFs — exclude 15m from agreement count (too noisy)
    agreeing = sum(1 for tf in ["1H", "4H", "1D"] if tf_scores[tf]["signal"] == signal and signal != 0)
    confidence = "HIGH" if agreeing == 3 else ("MEDIUM" if agreeing == 2 else "LOW")

    return {
        "signal": signal,
        "net_score": round(net, 3),
        "confidence": confidence,
        "agreeing_tfs": agreeing,
        "counter_trend_blocked": counter,
    }


def _calc_levels(direction: int, price: float, atr_4h: float, atr_1h: float, atr_15m: float = 0) -> dict:
    atr = atr_4h * 0.7 + atr_1h * 0.3
    # Entry zone uses 15m ATR for tighter limit order range
    zone_atr = atr_15m if atr_15m > 0 else atr_1h * 0.3
    if direction == 1:
        entry = price
        sl    = entry - atr * SL_ATR_MULT
        tp1   = entry + atr * TP1_ATR_MULT
        tp2   = entry + atr * TP2_ATR_MULT
        entry_zone = f"${round(price - zone_atr):,.0f} – ${round(price):,.0f}"
    else:
        entry = price
        sl    = entry + atr * SL_ATR_MULT
        tp1   = entry - atr * TP1_ATR_MULT
        tp2   = entry - atr * TP2_ATR_MULT
        entry_zone = f"${round(price):,.0f} – ${round(price + zone_atr):,.0f}"

    risk = abs(entry - sl)
    return {
        "entry": round(entry, 0),
        "sl": round(sl, 0),
        "tp1": round(tp1, 0),
        "tp2": round(tp2, 0),
        "risk_pct": round(risk / entry * 100, 2),
        "rr_tp1": round(abs(tp1 - entry) / risk, 2) if risk else 0,
        "rr_tp2": round(abs(tp2 - entry) / risk, 2) if risk else 0,
        "atr_used": round(atr, 0),
        "entry_limit_zone": entry_zone,
    }


# ── Display ───────────────────────────────────────────────────────────────────

def _print_signal(signal: TradeSignal, tf_scores: dict, confluence: dict, run_id: str = "") -> None:
    from rich import box
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    if signal.direction == 0:
        reason = "Counter-trend conflict" if confluence.get("counter_trend_blocked") else "Insufficient confluence"
        src = f"[dim]Source: {signal.source}[/dim]"
        display.console.print(Panel(
            f"[yellow]NO TRADE[/yellow]\n[dim]{reason}[/dim]\n{src}",
            border_style="yellow", title="[bold]BTC Trade Signal[/bold]",
        ))
        return

    dir_label = signal.direction_label
    dir_style = "bold green" if signal.direction == 1 else "bold red"
    border    = "green" if signal.direction == 1 else "red"

    src_badge = (
        "[cyan]vibe-trading agent[/cyan]" if signal.source == "agent"
        else "[dim]local multi-TF[/dim]"
    )

    header = Text()
    header.append(f"  {dir_label}  ", style=f"{dir_style}")
    header.append(f"  ${signal.entry:,.0f}  ", style="bold white")
    header.append(f"  Confidence: {signal.confidence}", style="dim")
    header.append(f"  ({confluence.get('agreeing_tfs', '?')}/3 TF agree)", style="dim")
    header.append(f"\n  Source: ", style="dim")
    header.append(src_badge)
    if run_id:
        header.append(f"  run:{run_id[:8]}", style="dim")

    display.console.print(Panel(header, border_style=border, title="[bold]BTC Trade Signal[/bold]"))

    # Levels
    risk_amount_example = signal.entry * (abs(signal.entry - signal.sl) / signal.entry)
    lvl = Table(box=box.ROUNDED, show_header=False, padding=(0, 1))
    lvl.add_column("", style="dim", width=20)
    lvl.add_column("", min_width=36)

    lvl.add_row("Entry (market)",  f"[bold]${signal.entry:,.0f}[/bold]")
    lvl.add_row("Stop Loss",
        f"[bold red]${signal.sl:,.0f}[/bold red]  "
        f"[dim]({abs(signal.entry - signal.sl) / signal.entry * 100:.2f}% from entry)[/dim]")

    rr1 = abs(signal.tp1 - signal.entry) / max(abs(signal.entry - signal.sl), 1)
    rr2 = abs(signal.tp2 - signal.entry) / max(abs(signal.entry - signal.sl), 1)
    lvl.add_row("TP1 (50%)",
        f"[bold green]${signal.tp1:,.0f}[/bold green]  [dim]R:R {rr1:.2f}:1[/dim]")
    lvl.add_row("TP2 (runner)",
        f"[bold green]${signal.tp2:,.0f}[/bold green]  [dim]R:R {rr2:.2f}:1[/dim]")

    display.console.print(lvl)

    # TF breakdown
    tf_t = Table(box=box.SIMPLE, title="Timeframe Breakdown", title_style="dim")
    tf_t.add_column("TF",  width=5, style="dim")
    tf_t.add_column("Signal",  width=8)
    tf_t.add_column("Strength", width=12)
    tf_t.add_column("RSI",  width=7)
    tf_t.add_column("ADX",  width=7)
    tf_t.add_column("EMA",  width=8)

    for tf in TIMEFRAMES:
        s = tf_scores[tf]
        sig = s["signal"]
        lbl = "LONG" if sig == 1 else ("SHORT" if sig == -1 else "FLAT")
        ss = "green" if sig == 1 else ("red" if sig == -1 else "yellow")
        es = "green" if s["ema_cross"] == "bull" else "red"
        tf_label = f"[dim]{tf}*[/dim]" if tf == "15m" else tf  # * = entry timing only
        tf_t.add_row(
            tf_label,
            f"[{ss}]{lbl}[/]",
            f"{'█' * (s['strength'] // 10):<10}",
            str(s["rsi"]),
            str(s["adx"]),
            f"[{es}]{'Bull' if s['ema_cross'] == 'bull' else 'Bear'}[/]",
        )
    display.console.print(tf_t)
    display.console.print("[dim]  * 15m = entry timing only (weight 0.5, not counted in confidence)[/dim]\n")

    # Agent reasoning
    if signal.agent_reasoning and signal.source == "agent":
        display.console.print(
            Panel(
                f"[dim]{signal.agent_reasoning}[/dim]",
                title="Agent Reasoning",
                border_style="dim",
            )
        )


def _print_sizing(balance: float, risk_pct: float, entry: float, sl: float) -> None:
    risk_amt = balance * risk_pct / 100
    price_risk = abs(entry - sl) / entry if entry else 0
    if price_risk > 0:
        btc_size = risk_amt / (price_risk * entry)
        notional = btc_size * entry
        display.console.print(
            f"\n[dim]Position sizing (${balance:.0f} balance, {risk_pct:.0f}% risk):[/dim]\n"
            f"  Risk ${risk_amt:.2f}  →  "
            f"[bold]{btc_size:.6f} BTC  (${notional:,.0f} notional)[/bold]\n"
        )


# ── Main command ──────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> None:
    symbol  = args.symbol
    swap_id = symbol.replace("-USDT", "-USDT-SWAP") if not symbol.endswith("-SWAP") else symbol

    # ── Phase 1: Parallel data fetch ─────────────────────────────────────────
    display.console.print(f"[dim]Fetching {symbol} data (15m / 1H / 4H / 1D + funding + OI)...[/dim]")

    results: dict = {}
    errors:  dict = {}

    def fetch(key, fn, *a, **kw):
        try:
            results[key] = fn(*a, **kw)
        except Exception as e:
            errors[key] = str(e)
            results[key] = None

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [
            pool.submit(fetch, "15m",    okx_client.get_ohlcv, symbol, "15m", 300),
            pool.submit(fetch, "1H",     okx_client.get_ohlcv, symbol, "1H",  300),
            pool.submit(fetch, "4H",     okx_client.get_ohlcv, symbol, "4H",  300),
            pool.submit(fetch, "1D",     okx_client.get_ohlcv, symbol, "1D",  300),
            pool.submit(fetch, "fund",   okx_client.get_funding_rate,         swap_id),
            pool.submit(fetch, "fund_h", okx_client.get_funding_rate_history, swap_id, 21),
            pool.submit(fetch, "oi",     okx_client.get_open_interest,        swap_id),
            pool.submit(fetch, "mark",   okx_client.get_mark_price,           swap_id),
            pool.submit(fetch, "spot",   okx_client.get_ticker,               symbol),
        ]
        for f in as_completed(futs):
            pass

    for tf in TIMEFRAMES:
        if results.get(tf) is None or results[tf].empty:
            display.console.print(f"[red]Missing OHLCV for {tf}: {errors.get(tf, 'empty')}[/red]")
            sys.exit(1)

    # ── Local scoring (always runs — used in prompt AND as fallback) ──────────
    tf_scores  = {tf: _score_tf(results[tf]) for tf in TIMEFRAMES}
    confluence = _confluence(tf_scores)

    price      = tf_scores["4H"]["close"]
    atr_4h     = tf_scores["4H"]["atr"]
    atr_1h     = tf_scores["1H"]["atr"]
    atr_15m    = tf_scores["15m"]["atr"]
    mark_price = float((results.get("mark") or {}).get("markPx", price))
    spot       = results.get("spot") or {}
    open_24h   = float(spot.get("open24h", price))
    change_24h = (price - open_24h) / open_24h * 100 if open_24h else 0

    # Funding data
    fund_raw  = results.get("fund") or {}
    fund_hist = results.get("fund_h") or []
    rate_8h   = float(fund_raw.get("fundingRate", 0))
    ann_pct   = rate_8h * 3 * 365 * 100
    hist_rates = [float(h["fundingRate"]) for h in fund_hist]
    avg_7d     = sum(hist_rates) / len(hist_rates) if hist_rates else 0.0
    regime     = _classify_regime(hist_rates)
    funding_data = {"rate_8h": rate_8h, "ann_pct": ann_pct, "avg_7d": avg_7d, "regime": regime}

    # OI data
    oi_raw  = results.get("oi") or {}
    oi_data = {"oi_usd_b": float(oi_raw.get("oiUsd", 0)) / 1e9}

    # Local levels (fallback)
    local_dir    = confluence["signal"]
    local_levels = _calc_levels(local_dir, price, atr_4h, atr_1h, atr_15m) if local_dir != 0 else None

    # ── --no-agent: skip agent entirely ──────────────────────────────────────
    if getattr(args, "no_agent", False):
        signal = local_fallback_signal(local_dir, confluence, local_levels, regime, price)
        if args.json:
            display.print_json(_signal_to_dict(signal, tf_scores, confluence))
        else:
            _print_signal(signal, tf_scores, confluence)
            if signal.direction != 0:
                _print_sizing(args.balance, args.risk_pct, signal.entry, signal.sl)
        return

    # ── Phase 2: Agent availability check ────────────────────────────────────
    agent_ready, agent_reason = check_agent_configured()
    if not agent_ready:
        display.console.print(f"[yellow]Agent not configured: {agent_reason}[/yellow]")
        display.console.print("[dim]Using local multi-TF analysis...[/dim]\n")
        signal = local_fallback_signal(local_dir, confluence, local_levels, regime, price)
        if args.json:
            display.print_json(_signal_to_dict(signal, tf_scores, confluence))
        else:
            _print_signal(signal, tf_scores, confluence)
            if signal.direction != 0:
                _print_sizing(args.balance, args.risk_pct, signal.entry, signal.sl)
        return

    # ── Phase 3: Agent analysis ───────────────────────────────────────────────
    display.console.print("[cyan]Running vibe-trading agent (perp-funding + liq-heatmap + onchain + smc)...[/cyan]")
    display.console.print("[dim]This takes 60-120 seconds with Gemini...[/dim]\n")

    snapshot = build_market_snapshot(tf_scores, confluence, funding_data, oi_data,
                                     price, mark_price, change_24h)
    prompt   = build_prompt(snapshot, price)

    agent_result = run_agent_subprocess(prompt)

    if agent_result["status"] != "success":
        err = agent_result.get("error", "unknown")
        display.console.print(f"[yellow]Agent failed: {err}[/yellow]")
        display.console.print("[dim]Falling back to local analysis...[/dim]\n")
        signal = local_fallback_signal(local_dir, confluence, local_levels, regime, price)
    else:
        signal = parse_agent_output(agent_result["content"], price)
        if signal is None:
            display.console.print("[yellow]Could not parse agent output — using local fallback[/yellow]\n")
            signal = local_fallback_signal(local_dir, confluence, local_levels, regime, price)

    # ── Phase 4: Display ─────────────────────────────────────────────────────
    run_id = agent_result.get("run_id", "") if agent_result["status"] == "success" else ""

    if args.json:
        display.print_json(_signal_to_dict(signal, tf_scores, confluence, run_id))
    else:
        _print_signal(signal, tf_scores, confluence, run_id)
        if signal.direction != 0:
            _print_sizing(args.balance, args.risk_pct, signal.entry, signal.sl)


def _signal_to_dict(signal: TradeSignal, tf_scores: dict, confluence: dict, run_id: str = "") -> dict:
    return {
        "direction": signal.direction_label,
        "entry": signal.entry,
        "sl": signal.sl,
        "tp1": signal.tp1,
        "tp2": signal.tp2,
        "confidence": signal.confidence,
        "source": signal.source,
        "run_id": run_id,
        "reasoning": signal.agent_reasoning,
        "confluence": confluence,
        "timeframes": tf_scores,
    }
