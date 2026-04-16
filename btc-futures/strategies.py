"""Named SignalEngine classes for BTC futures backtesting.

Each class implements the SignalEngine contract:
    generate(data_map: dict[str, DataFrame]) -> dict[str, Series]

Signal values: +1.0 = fully long, -1.0 = fully short, 0.0 = flat.
The backtest engine shifts signals by 1 bar (next-bar-open semantics).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from indicators import compute_all, ema, hv_percentile


class FundingMeanRevertEngine:
    """Mean-reversion based on RSI + Bollinger Band extremes.

    Hypothesis: extreme RSI + price outside BB = overcrowded position,
    fade the crowd. Proxies funding-rate regime via RSI/price divergence.

    Long entry:  RSI < 35 AND close < lower BB
    Short entry: RSI > 65 AND close > upper BB
    Exit:        RSI crosses back through 50, or opposite signal fires
    """

    def __init__(self, rsi_low: float = 35, rsi_high: float = 65):
        self.rsi_low = rsi_low
        self.rsi_high = rsi_high

    def generate(self, data_map: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
        signals = {}
        for code, df in data_map.items():
            tech = compute_all(df)
            close = tech["close"]
            rsi_s = tech["rsi"]
            bb_lower = tech["bb_lower"]
            bb_upper = tech["bb_upper"]

            raw = pd.Series(0.0, index=close.index)
            raw[rsi_s < self.rsi_low] = 1.0
            raw[rsi_s > self.rsi_high] = -1.0

            # Only fire when price confirms (outside BB)
            long_ok = close < bb_lower
            short_ok = close > bb_upper
            signal = pd.Series(0.0, index=close.index)
            signal[(raw == 1.0) & long_ok] = 1.0
            signal[(raw == -1.0) & short_ok] = -1.0

            # Exit when RSI returns to neutral zone (40–60)
            neutral = (rsi_s > 40) & (rsi_s < 60)
            # Carry signal forward until neutral zone hit
            carried = signal.copy()
            for i in range(1, len(carried)):
                if signal.iloc[i] == 0.0 and not neutral.iloc[i]:
                    carried.iloc[i] = carried.iloc[i - 1]

            signals[code] = carried.clip(-1.0, 1.0)
        return signals


class TrendFollowEngine:
    """EMA crossover + ADX trend filter + OBV confirmation.

    Long:  EMA(12) > EMA(26) AND ADX > threshold AND DI+ > DI- AND OBV > OBV_EMA(20)
    Short: EMA(12) < EMA(26) AND ADX > threshold AND DI- > DI+ AND OBV < OBV_EMA(20)
    Flat:  ADX < 20 (choppy market, no position)
    """

    def __init__(self, adx_threshold: float = 25, adx_min: float = 20):
        self.adx_threshold = adx_threshold
        self.adx_min = adx_min

    def generate(self, data_map: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
        signals = {}
        for code, df in data_map.items():
            tech = compute_all(df)

            ema_bull = tech["ema_12"] > tech["ema_26"]
            ema_bear = tech["ema_12"] < tech["ema_26"]
            adx_strong = tech["adx"] > self.adx_threshold
            adx_flat = tech["adx"] < self.adx_min
            di_long = tech["di_plus"] > tech["di_minus"]
            di_short = tech["di_minus"] > tech["di_plus"]
            obv_bull = tech["obv"] > tech["obv_ema20"]
            obv_bear = tech["obv"] < tech["obv_ema20"]

            signal = pd.Series(0.0, index=df.index)
            signal[ema_bull & adx_strong & di_long & obv_bull] = 1.0
            signal[ema_bear & adx_strong & di_short & obv_bear] = -1.0
            signal[adx_flat] = 0.0  # flat market override

            signals[code] = signal
        return signals


class VolatilityRegimeEngine:
    """Enter long when vol is compressed (expansion expected), short when vol spikes.

    Uses HV(20) percentile over 120-bar lookback:
    - HV_pct < low_pct  → long  (vol crush, expansion likely)
    - HV_pct > high_pct → short (vol spike, mean-reversion)
    - Otherwise         → flat

    Max hold: 10 bars (time stop to prevent stale positions).
    """

    def __init__(self, low_pct: float = 20, high_pct: float = 80, max_hold: int = 10):
        self.low_pct = low_pct
        self.high_pct = high_pct
        self.max_hold = max_hold

    def generate(self, data_map: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
        signals = {}
        for code, df in data_map.items():
            hv_pct = hv_percentile(df["close"], hv_window=20, lookback=120)

            raw = pd.Series(0.0, index=df.index)
            raw[hv_pct < self.low_pct] = 1.0
            raw[hv_pct > self.high_pct] = -1.0

            # Apply time stop: exit after max_hold bars
            final = pd.Series(0.0, index=df.index)
            hold_count = 0
            current_dir = 0.0
            for i, (idx, sig) in enumerate(raw.items()):
                if sig != 0.0 and sig != current_dir:
                    current_dir = sig
                    hold_count = 0
                if current_dir != 0.0:
                    hold_count += 1
                    if hold_count <= self.max_hold:
                        final.iloc[i] = current_dir
                    else:
                        current_dir = 0.0
                        hold_count = 0

            signals[code] = final
        return signals


class OITrendEngine:
    """Open interest / volume trend: rising OI + rising price = conviction long.

    Since backtest OHLCV doesn't carry live OI, uses volume as proxy:
    - Volume ROC > 10% AND Price ROC > 3%  → long  (new money flowing in, price rising)
    - Volume ROC > 10% AND Price ROC < -3% → short (new money flowing in, price falling)
    - Volume ROC < 0%                      → flat  (OI declining, no conviction)

    ROC computed over 5-bar rolling window.
    """

    def __init__(self, vol_roc_thresh: float = 0.10, price_roc_thresh: float = 0.03, window: int = 5):
        self.vol_roc_thresh = vol_roc_thresh
        self.price_roc_thresh = price_roc_thresh
        self.window = window

    def generate(self, data_map: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
        signals = {}
        for code, df in data_map.items():
            close = df["close"]
            volume = df["volume"]

            price_roc = close.pct_change(self.window)
            vol_roc = volume.pct_change(self.window)

            signal = pd.Series(0.0, index=df.index)
            signal[(vol_roc > self.vol_roc_thresh) & (price_roc > self.price_roc_thresh)] = 1.0
            signal[(vol_roc > self.vol_roc_thresh) & (price_roc < -self.price_roc_thresh)] = -1.0
            signal[vol_roc < 0] = 0.0  # declining OI → exit

            signals[code] = signal
        return signals


STRATEGY_REGISTRY: dict[str, type] = {
    "funding-mean-revert": FundingMeanRevertEngine,
    "trend-follow": TrendFollowEngine,
    "vol-regime": VolatilityRegimeEngine,
    "oi-trend": OITrendEngine,
}
