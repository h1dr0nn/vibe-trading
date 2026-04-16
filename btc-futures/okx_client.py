"""OKX V5 public REST API client — no authentication required."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import requests

BASE_URL = "https://www.okx.com/api/v5"
TIMEOUT = 10


class OKXAPIError(Exception):
    pass


def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    url = BASE_URL + path
    resp = requests.get(url, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "0":
        raise OKXAPIError(f"OKX API error {data.get('code')}: {data.get('msg')}")
    return data["data"]


def get_ticker(inst_id: str = "BTC-USDT") -> dict:
    """Spot ticker: last, open24h, high24h, low24h, vol24h."""
    data = _get("/market/ticker", {"instId": inst_id})
    return data[0] if data else {}


def get_funding_rate(inst_id: str = "BTC-USDT-SWAP") -> dict:
    """Current funding rate for a perpetual swap."""
    data = _get("/public/funding-rate", {"instId": inst_id})
    return data[0] if data else {}


def get_funding_rate_history(inst_id: str = "BTC-USDT-SWAP", limit: int = 21) -> list[dict]:
    """Historical funding rates. 21 periods = 7 days (3 settlements/day)."""
    return _get("/public/funding-rate-history", {"instId": inst_id, "limit": str(limit)})


def get_open_interest(inst_id: str = "BTC-USDT-SWAP") -> dict:
    """Open interest in contracts and USD notional."""
    data = _get("/public/open-interest", {"instType": "SWAP", "instId": inst_id})
    return data[0] if data else {}


def get_mark_price(inst_id: str = "BTC-USDT-SWAP") -> dict:
    """Mark price for a perpetual swap."""
    data = _get("/public/mark-price", {"instType": "SWAP", "instId": inst_id})
    return data[0] if data else {}


def get_index_ticker(inst_id: str = "BTC-USD") -> dict:
    """Spot index price (composite, different from mark price)."""
    data = _get("/market/index-tickers", {"instId": inst_id})
    return data[0] if data else {}


def get_ohlcv(inst_id: str = "BTC-USDT", bar: str = "4H", limit: int = 200) -> pd.DataFrame:
    """Fetch the latest N OHLCV bars.

    Returns DataFrame with DatetimeIndex and columns: open, high, low, close, volume.
    OKX returns newest-first; we reverse to chronological order.
    """
    raw = _get("/market/candles", {"instId": inst_id, "bar": bar, "limit": str(limit)})
    if not raw:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    rows = []
    for c in raw:
        ts = pd.Timestamp(int(c[0]), unit="ms", tz="UTC")
        rows.append({
            "timestamp": ts,
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5]),
        })

    df = pd.DataFrame(rows).set_index("timestamp").sort_index()
    return df


def get_instruments_futures(base: str = "BTC") -> list[dict]:
    """List active BTC quarterly futures (for basis/term-structure calc)."""
    instruments = _get("/public/instruments", {"instType": "FUTURES"})
    return [i for i in instruments if i.get("ctValCcy", "").upper() == base or
            i.get("instId", "").startswith(base)]


def get_futures_ticker(inst_id: str) -> dict:
    """Ticker for a specific quarterly futures contract."""
    data = _get("/market/ticker", {"instId": inst_id})
    return data[0] if data else {}


def get_nearest_quarterly_basis(base: str = "BTC") -> dict | None:
    """Fetch the nearest quarterly future and compute basis vs spot.

    Returns dict with keys: inst_id, expiry, futures_price, spot_price,
    basis_pct, annualized_basis_pct, days_to_expiry.
    """
    instruments = get_instruments_futures(base)
    if not instruments:
        return None

    now_ts = int(time.time() * 1000)
    valid = [
        i for i in instruments
        if i.get("instId", "").startswith(f"{base}-USDT-") and
        i.get("state") == "live" and
        int(i.get("expTime", 0)) > now_ts
    ]
    if not valid:
        return None

    nearest = min(valid, key=lambda i: int(i.get("expTime", 9e18)))
    inst_id = nearest["instId"]
    exp_ts = int(nearest["expTime"])
    exp_dt = datetime.fromtimestamp(exp_ts / 1000, tz=timezone.utc)
    days_to_expiry = max((exp_dt - datetime.now(tz=timezone.utc)).days, 1)

    futures_ticker = get_futures_ticker(inst_id)
    spot_ticker = get_ticker(f"{base}-USDT")

    futures_price = float(futures_ticker.get("last", 0))
    spot_price = float(spot_ticker.get("last", 0))

    if spot_price == 0:
        return None

    basis_pct = (futures_price - spot_price) / spot_price * 100
    annualized = basis_pct * 365 / days_to_expiry

    return {
        "inst_id": inst_id,
        "expiry": exp_dt.strftime("%Y-%m-%d"),
        "futures_price": futures_price,
        "spot_price": spot_price,
        "basis_pct": round(basis_pct, 4),
        "annualized_basis_pct": round(annualized, 2),
        "days_to_expiry": days_to_expiry,
    }
