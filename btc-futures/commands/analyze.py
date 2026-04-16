"""btc analyze — full composite BTC futures market analysis."""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, __import__("pathlib").Path(__file__).resolve().parents[1].__str__())

import display
import indicators
import okx_client
from commands.funding import _classify_regime


# ── Technical scoring ─────────────────────────────────────────────────────────

def _score_technical(tech_4h, tech_1d: object) -> dict:
    """Score -1 to +1 from multi-indicator vote."""
    latest = tech_4h.iloc[-1]
    latest_1d = tech_1d.iloc[-1]

    votes = []

    # EMA cross (both timeframes)
    ema_cross_4h = 1 if latest["ema_12"] > latest["ema_26"] else -1
    ema_cross_1d = 1 if latest_1d["ema_12"] > latest_1d["ema_26"] else -1
    votes.extend([ema_cross_4h * 1.5, ema_cross_1d * 1.0])  # 1D has more weight

    # ADX direction filter
    if latest["adx"] > 25:
        di_dir = 1 if latest["di_plus"] > latest["di_minus"] else -1
        votes.append(di_dir * 1.0)

    # RSI signal (not overbought/oversold — directional bias)
    rsi = latest["rsi"]
    if rsi > 55:
        votes.append(0.5)
    elif rsi < 45:
        votes.append(-0.5)

    # BB position
    bb_pct = latest["bb_pct"]
    if bb_pct > 0.8:
        votes.append(-0.5)  # near upper band = overbought bias
    elif bb_pct < 0.2:
        votes.append(0.5)   # near lower band = oversold bias

    # OBV trend
    obv_dir = 1 if latest["obv"] > latest["obv_ema20"] else -1
    votes.append(obv_dir * 0.5)

    score = sum(votes) / (sum(abs(v) for v in votes) or 1)  # normalise to [-1, +1]
    signal = 1 if score > 0.15 else (-1 if score < -0.15 else 0)

    ema_cross = "bullish" if ema_cross_4h == 1 else "bearish"
    bb_position = "upper" if bb_pct > 0.7 else ("lower" if bb_pct < 0.3 else "mid")

    return {
        "signal": signal,
        "score": round(score, 3),
        "ema_cross": ema_cross,
        "rsi": round(float(rsi), 1),
        "adx": round(float(latest["adx"]), 1),
        "bb_position": bb_position,
        "bb_pct": round(float(bb_pct), 3) if not _isnan(bb_pct) else 0.5,
        "obv_trend": "rising" if obv_dir == 1 else "falling",
        "atr": round(float(latest["atr"]), 0),
        "hv_pct": round(float(latest["hv_pct"]), 1) if not _isnan(latest["hv_pct"]) else 50.0,
    }


def _isnan(v) -> bool:
    try:
        import math
        return math.isnan(float(v))
    except Exception:
        return True


# ── Funding scoring ───────────────────────────────────────────────────────────

def _score_funding(current: dict, history: list[dict]) -> dict:
    """Classify funding regime and derive directional signal.

    Regime thresholds from perp-funding-basis/SKILL.md:
    - overheated_long  → mean-revert short signal (-1)
    - bullish_carry    → cautious, mild short (-0.5)
    - neutral          → no funding signal (0)
    - bearish_carry    → cautious, mild long (+0.5)
    - overheated_short → mean-revert long signal (+1)
    """
    rate_8h = float(current.get("fundingRate", 0))
    annualized = rate_8h * 3 * 365 * 100
    hist_rates = [float(h["fundingRate"]) for h in history]
    avg_7d = sum(hist_rates) / len(hist_rates) if hist_rates else 0.0
    regime = _classify_regime(hist_rates)

    # Count consecutive same-direction settlements
    consecutive = 0
    if hist_rates:
        first_sign = 1 if hist_rates[0] > 0 else -1
        for r in hist_rates:
            if (1 if r > 0 else -1) == first_sign:
                consecutive += 1
            else:
                break

    signal_map = {
        "overheated_long":  -1,
        "bullish_carry":    -0.5,
        "neutral":           0,
        "bearish_carry":     0.5,
        "overheated_short":  1,
        "mixed":             0,
        "unknown":           0,
    }
    signal = signal_map.get(regime, 0)

    return {
        "signal": signal,
        "regime": regime,
        "rate_8h": round(rate_8h, 6),
        "annualized": round(annualized, 2),
        "avg_7d": round(avg_7d, 6),
        "consecutive_dir": consecutive,
    }


# ── Liquidation level scoring ─────────────────────────────────────────────────

def _score_liquidation(ticker: dict, oi: dict, ohlcv_1d) -> dict:
    """Estimate liquidation cluster positions and cascade risk.

    Approach (approximation — no CoinGlass API required):
    - Use 30-day rolling high/low as assumed long/short entry zones
    - Estimate liquidation prices at common leverage levels (5x, 10x, 20x)
    - If price is approaching a large cluster: magnet signal
    """
    price = float(ticker.get("last", 0))
    high_30d = float(ohlcv_1d["high"].tail(30).max())
    low_30d = float(ohlcv_1d["low"].tail(30).min())
    mid_30d = (high_30d + low_30d) / 2

    # Estimated long liquidation levels (longs entered near highs, liquidated below)
    long_liq_levels = {
        "5x":  high_30d * (1 - 1 / 5),
        "10x": high_30d * (1 - 1 / 10),
        "20x": high_30d * (1 - 1 / 20),
    }
    # Estimated short liquidation levels (shorts entered near lows, liquidated above)
    short_liq_levels = {
        "5x":  low_30d * (1 + 1 / 5),
        "10x": low_30d * (1 + 1 / 10),
        "20x": low_30d * (1 + 1 / 20),
    }

    # Distance from current price to nearest cluster
    dist_to_long_liq = min(abs(price - v) / price for v in long_liq_levels.values())
    dist_to_short_liq = min(abs(price - v) / price for v in short_liq_levels.values())

    # Cascade risk: if price within 3% of a significant cluster
    cascade_risk = "high" if min(dist_to_long_liq, dist_to_short_liq) < 0.03 else (
        "medium" if min(dist_to_long_liq, dist_to_short_liq) < 0.07 else "low"
    )

    # Magnet direction: price closer to which cluster?
    if dist_to_long_liq < dist_to_short_liq:
        bias = "downward_magnet"  # long liquidations below = gravity pull down
    elif dist_to_short_liq < dist_to_long_liq:
        bias = "upward_magnet"    # short liquidations above = short squeeze magnet
    else:
        bias = "balanced"

    return {
        "bias": bias,
        "cascade_risk": cascade_risk,
        "long_liq_5x": round(long_liq_levels["5x"], 0),
        "long_liq_10x": round(long_liq_levels["10x"], 0),
        "short_liq_5x": round(short_liq_levels["5x"], 0),
        "short_liq_10x": round(short_liq_levels["10x"], 0),
        "dist_to_long_liq_pct": round(dist_to_long_liq * 100, 2),
        "dist_to_short_liq_pct": round(dist_to_short_liq * 100, 2),
        "note": "Estimated from 30d rolling high/low; not real CoinGlass data",
    }


# ── On-chain proxy scoring ────────────────────────────────────────────────────

def _score_onchain_proxy(ticker: dict, oi: dict, ohlcv_1d) -> dict:
    """Approximate on-chain composite score using OKX public data only.

    Dimensions (all proxied):
    - Valuation:    price vs 200-day SMA (proxy for MVRV)
    - Activity:     volume vs 30-day avg volume (proxy for active addresses)
    - Capital flow: OI USD change (proxy for stablecoin inflows)
    - Whale:        single-day volume spike (proxy for whale accumulation)

    Score: 1–5 (1=bearish, 3=neutral, 5=bullish)
    """
    price = float(ticker.get("last", 0))
    vol_24h = float(ticker.get("volCcy24h", ticker.get("vol24h", 0)))

    close = ohlcv_1d["close"]
    volume = ohlcv_1d["volume"]

    sma200 = close.tail(200).mean()
    avg_vol_30 = volume.tail(30).mean()
    latest_vol = float(volume.iloc[-1])

    # 1. Valuation proxy (price vs 200d SMA)
    price_vs_sma = price / sma200 if sma200 > 0 else 1.0
    val_score = 3
    if price_vs_sma > 1.5:
        val_score = 2   # expensive
    elif price_vs_sma > 1.1:
        val_score = 3
    elif price_vs_sma < 0.8:
        val_score = 5   # undervalued
    elif price_vs_sma < 0.95:
        val_score = 4

    # 2. Activity proxy (volume spike)
    vol_ratio = latest_vol / avg_vol_30 if avg_vol_30 > 0 else 1.0
    activity_score = 3
    if vol_ratio > 2.0:
        # High volume — direction determines score
        price_change_1d = float(close.pct_change().iloc[-1])
        activity_score = 4 if price_change_1d > 0 else 2
    elif vol_ratio < 0.5:
        activity_score = 3  # low activity, neutral

    # 3. Capital flow proxy (OI change)
    oi_usd = float(oi.get("oiUsd", 0))
    oi_score = 3  # neutral default (no historical OI for delta)

    # 4. Composite
    composite = (val_score + activity_score + oi_score) / 3
    oi_change_pct = 0.0  # would need previous OI fetch for real delta
    vol_price_div = "aligned" if (vol_ratio > 1 and float(close.pct_change().iloc[-1]) > 0) or \
                                  (vol_ratio > 1 and float(close.pct_change().iloc[-1]) < 0) else "neutral"

    return {
        "score": round(composite, 1),
        "val_score": val_score,
        "activity_score": activity_score,
        "price_vs_sma200": round(price_vs_sma, 3),
        "vol_ratio_30d": round(vol_ratio, 2),
        "oi_usd": round(oi_usd, 0),
        "oi_change_pct": oi_change_pct,
        "vol_price_div": vol_price_div,
        "note": "Proxy metrics — no Glassnode/Nansen API",
    }


# ── Composite bias ────────────────────────────────────────────────────────────

def _composite_bias(tech: dict, funding: dict, liq: dict, onchain: dict) -> dict:
    weights = {"technical": 0.35, "funding": 0.30, "liquidation": 0.20, "onchain": 0.15}

    # Normalise each component to [-1, +1]
    tech_s = float(tech.get("signal", 0))
    fund_s = float(funding.get("signal", 0))

    liq_raw = liq.get("bias", "balanced")
    liq_s = 1.0 if liq_raw == "upward_magnet" else (-1.0 if liq_raw == "downward_magnet" else 0.0)

    oc_score = float(onchain.get("score", 3))
    oc_s = (oc_score - 3) / 2  # maps 1–5 to -1..+1

    score = (
        tech_s * weights["technical"] +
        fund_s * weights["funding"] +
        liq_s  * weights["liquidation"] +
        oc_s   * weights["onchain"]
    )

    bias = "long" if score > 0.1 else ("short" if score < -0.1 else "neutral")
    abs_score = abs(score)
    confidence = "high" if abs_score > 0.4 else ("medium" if abs_score > 0.2 else "low")

    return {"bias": bias, "score": round(score, 3), "confidence": confidence}


# ── Main command ──────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> None:
    symbol = args.symbol
    swap_id = symbol.replace("-USDT", "-USDT-SWAP") if not symbol.endswith("-SWAP") else symbol
    timeframe = args.timeframe

    display.console.print(f"[dim]Fetching data for {symbol}...[/dim]")

    # Parallel data fetch
    results = {}
    errors = []

    def fetch(key, fn, *fn_args, **fn_kwargs):
        try:
            results[key] = fn(*fn_args, **fn_kwargs)
        except Exception as e:
            errors.append(f"{key}: {e}")
            results[key] = None

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(fetch, "ticker",       okx_client.get_ticker,              symbol): "ticker",
            pool.submit(fetch, "funding",       okx_client.get_funding_rate,        swap_id): "funding",
            pool.submit(fetch, "funding_hist",  okx_client.get_funding_rate_history,swap_id, 21): "funding_hist",
            pool.submit(fetch, "oi",            okx_client.get_open_interest,       swap_id): "oi",
            pool.submit(fetch, "ohlcv_tf",      okx_client.get_ohlcv,               symbol, timeframe, 300): "ohlcv_tf",
            pool.submit(fetch, "ohlcv_1d",      okx_client.get_ohlcv,               symbol, "1D", 365): "ohlcv_1d",
        }
        for f in as_completed(futures):
            pass  # results populated via side-effect in fetch()

    if errors:
        for e in errors:
            display.console.print(f"[yellow]Warning — {e}[/yellow]")

    # Validate critical data
    if results.get("ohlcv_tf") is None or results["ohlcv_tf"].empty:
        display.console.print("[red]Failed to fetch OHLCV data. Check network.[/red]")
        sys.exit(1)

    tech_tf = indicators.compute_all(results["ohlcv_tf"])
    tech_1d = indicators.compute_all(results["ohlcv_1d"]) if results.get("ohlcv_1d") is not None else tech_tf

    tech = _score_technical(tech_tf, tech_1d)
    funding = _score_funding(
        results.get("funding") or {},
        results.get("funding_hist") or [],
    )
    liq = _score_liquidation(
        results.get("ticker") or {},
        results.get("oi") or {},
        results["ohlcv_1d"] if results.get("ohlcv_1d") is not None else results["ohlcv_tf"],
    )
    onchain = _score_onchain_proxy(
        results.get("ticker") or {},
        results.get("oi") or {},
        results["ohlcv_1d"] if results.get("ohlcv_1d") is not None else results["ohlcv_tf"],
    )
    composite = _composite_bias(tech, funding, liq, onchain)

    if args.json:
        display.print_json({
            "composite": composite,
            "technical": tech,
            "funding": funding,
            "liquidation": liq,
            "onchain": onchain,
        })
    else:
        display.analyze_panel(composite, tech, funding, liq, onchain)
