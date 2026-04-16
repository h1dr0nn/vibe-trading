"""Pure-pandas technical indicators for BTC analysis.

All functions operate on standard OHLCV DataFrames.
Crypto uses 365-day annualisation for volatility metrics.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

CRYPTO_DAYS_PER_YEAR = 365


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI using EWM (consistent with Vibe-Trading agent skill)."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.DataFrame:
    """ADX with +DI and -DI. Returns DataFrame with columns: di_plus, di_minus, adx."""
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    up_move = high - prev_high
    down_move = prev_low - low

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr_s = pd.Series(plus_dm, index=close.index).ewm(alpha=1 / period, adjust=False).mean()
    atr_smooth = tr.ewm(alpha=1 / period, adjust=False).mean()
    minus_dm_s = pd.Series(minus_dm, index=close.index).ewm(alpha=1 / period, adjust=False).mean()

    di_plus = 100 * atr_s / atr_smooth.replace(0, np.nan)
    di_minus = 100 * minus_dm_s / atr_smooth.replace(0, np.nan)

    dx = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan)
    adx_s = dx.ewm(alpha=1 / period, adjust=False).mean()

    return pd.DataFrame({"di_plus": di_plus, "di_minus": di_minus, "adx": adx_s})


def bollinger_bands(close: pd.Series, window: int = 20, std: float = 2.0) -> pd.DataFrame:
    mid = close.rolling(window).mean()
    sigma = close.rolling(window).std()
    return pd.DataFrame({
        "bb_upper": mid + std * sigma,
        "bb_mid": mid,
        "bb_lower": mid - std * sigma,
        "bb_width": (2 * std * sigma) / mid.replace(0, np.nan),
        "bb_pct": (close - (mid - std * sigma)) / (2 * std * sigma).replace(0, np.nan),
    })


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    direction = np.sign(close.diff().fillna(0))
    return (volume * direction).cumsum()


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def historical_volatility(close: pd.Series, window: int = 20) -> pd.Series:
    """Annualised HV using 365 days (crypto)."""
    log_ret = np.log(close / close.shift(1))
    return log_ret.rolling(window).std() * np.sqrt(CRYPTO_DAYS_PER_YEAR)


def hv_percentile(close: pd.Series, hv_window: int = 20, lookback: int = 120) -> pd.Series:
    """Rolling percentile rank of HV over a lookback window."""
    hv = historical_volatility(close, hv_window)
    return hv.rolling(lookback).apply(
        lambda x: pd.Series(x).rank(pct=True).iloc[-1] * 100, raw=False
    )


def compute_all(df: pd.DataFrame) -> pd.DataFrame:
    """Compute all indicators and append as columns. Returns enriched DataFrame."""
    out = df.copy()
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]

    out["ema_12"] = ema(close, 12)
    out["ema_26"] = ema(close, 26)
    out["ema_50"] = ema(close, 50)
    out["rsi"] = rsi(close)

    adx_df = adx(high, low, close)
    out["adx"] = adx_df["adx"]
    out["di_plus"] = adx_df["di_plus"]
    out["di_minus"] = adx_df["di_minus"]

    bb = bollinger_bands(close)
    out["bb_upper"] = bb["bb_upper"]
    out["bb_mid"] = bb["bb_mid"]
    out["bb_lower"] = bb["bb_lower"]
    out["bb_pct"] = bb["bb_pct"]   # 0=lower band, 1=upper band
    out["bb_width"] = bb["bb_width"]

    out["obv"] = obv(close, volume)
    out["obv_ema20"] = ema(out["obv"], 20)
    out["atr"] = atr(high, low, close)
    out["hv20"] = historical_volatility(close, 20)
    out["hv_pct"] = hv_percentile(close, 20, 120)

    return out
