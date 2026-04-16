"""OKX V5 private REST API client — authenticated endpoints only.

Reads credentials from environment variables:
    OKX_API_KEY, OKX_SECRET_KEY, OKX_API_PASSPHRASE, OKX_DEMO_MODE
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from datetime import datetime, timezone
from typing import Any

import requests

from bot.okx_errors import (
    NetworkTimeoutError,
    OKXError,
    RateLimitError,
    ServerError,
    classify_okx_code,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://www.okx.com/api/v5"
TIMEOUT = 15


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _timestamp() -> str:
    """ISO 8601 UTC timestamp required by OKX."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _sign(secret: str, timestamp: str, method: str, path: str, body: str = "") -> str:
    """HMAC-SHA256 signature: base64(HMAC(secret, ts+method+path+body))."""
    prehash = timestamp + method.upper() + path + body
    sig = hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).digest()
    return base64.b64encode(sig).decode()


def _headers(method: str, path: str, body: str = "") -> dict[str, str]:
    api_key = os.environ["OKX_API_KEY"]
    secret = os.environ["OKX_SECRET_KEY"]
    passphrase = os.environ["OKX_API_PASSPHRASE"]
    demo = os.getenv("OKX_DEMO_MODE", "false").lower() == "true"

    ts = _timestamp()
    headers = {
        "OK-ACCESS-KEY": api_key,
        "OK-ACCESS-SIGN": _sign(secret, ts, method, path, body),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
    }
    if demo:
        headers["x-simulated-trading"] = "1"
    return headers


# ── Low-level request ─────────────────────────────────────────────────────────

def _request(method: str, path: str, payload: dict | None = None) -> Any:
    """Execute an authenticated request and return response data list."""
    import json as _json

    body = _json.dumps(payload) if payload else ""
    url = BASE_URL + path
    hdrs = _headers(method, path, body)

    try:
        if method == "GET":
            resp = requests.get(url, params=payload, headers=hdrs, timeout=TIMEOUT)
        else:
            resp = requests.post(url, data=body, headers=hdrs, timeout=TIMEOUT)
    except requests.Timeout as exc:
        raise NetworkTimeoutError(str(exc)) from exc
    except requests.ConnectionError as exc:
        raise NetworkTimeoutError(str(exc)) from exc

    if resp.status_code == 429:
        retry_after = float(resp.headers.get("Retry-After", 2))
        raise RateLimitError(retry_after)
    if resp.status_code >= 500:
        raise ServerError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    if resp.status_code >= 400:
        raise OKXError(f"HTTP {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    code = data.get("code", "0")
    if code != "0":
        msg = data.get("msg", "")
        # Some batch endpoints return per-item errors — log and raise
        logger.debug("OKX response: %s", data)
        raise classify_okx_code(code, msg)

    return data.get("data", [])


def _get(path: str, params: dict | None = None) -> Any:
    return _request("GET", path, params)


def _post(path: str, payload: dict) -> Any:
    return _request("POST", path, payload)


# ── Account ───────────────────────────────────────────────────────────────────

def get_balance(ccy: str = "USDT") -> float:
    """Return available equity for the given currency."""
    data = _get("/account/balance", {"ccy": ccy})
    if not data:
        return 0.0
    for detail in data[0].get("details", []):
        if detail.get("ccy") == ccy:
            return float(detail.get("eq", 0))
    return 0.0


def get_position(inst_id: str) -> dict | None:
    """Return the open position for inst_id, or None if no position."""
    data = _get("/account/positions", {"instType": "SWAP", "instId": inst_id})
    for pos in data:
        if pos.get("instId") == inst_id and float(pos.get("pos", 0)) != 0:
            return pos
    return None


def get_account_leverage(inst_id: str) -> int | None:
    """Return current leverage for inst_id (cross margin)."""
    data = _get("/account/leverage-info", {"instId": inst_id, "mgnMode": "cross"})
    if data:
        return int(float(data[0].get("lever", 0)))
    return None


def set_leverage(inst_id: str, lever: int) -> None:
    """Set leverage for inst_id on cross margin."""
    _post("/account/set-leverage", {
        "instId": inst_id,
        "lever": str(lever),
        "mgnMode": "cross",
    })
    logger.info("Leverage set to %dx for %s", lever, inst_id)


# ── Orders ────────────────────────────────────────────────────────────────────

def place_order(
    inst_id: str,
    side: str,
    size: str,
    order_type: str = "limit",
    price: str | None = None,
) -> str:
    """Place a limit or market order. Returns ordId."""
    payload: dict[str, Any] = {
        "instId": inst_id,
        "tdMode": "cross",
        "side": side,
        "ordType": order_type,
        "sz": size,
    }
    if order_type == "limit" and price:
        payload["px"] = price

    data = _post("/trade/order", payload)
    if not data:
        raise OKXError("place_order returned empty response")
    ord_id = data[0].get("ordId", "")
    if not ord_id or data[0].get("sCode", "0") != "0":
        raise OKXError(f"place_order failed: {data[0]}")
    logger.info("Placed %s %s order %s @ %s size=%s", order_type, side, ord_id, price, size)
    return ord_id


def set_algo_tp_sl(
    inst_id: str,
    ord_id: str,
    tp_price: str,
    sl_price: str,
) -> str:
    """Attach OCO algo TP/SL to an existing order. Returns algoId."""
    payload = {
        "instId": inst_id,
        "tdMode": "cross",
        "algoType": "oco",
        "tpTriggerPx": tp_price,
        "tpOrdPx": "-1",   # market order on trigger
        "slTriggerPx": sl_price,
        "slOrdPx": "-1",   # market order on trigger
        "attachAlgoOrds": "true",
        "ordId": ord_id,
    }
    data = _post("/trade/order-algo", payload)
    if not data:
        raise OKXError("set_algo_tp_sl returned empty response")
    algo_id = data[0].get("algoId", "")
    if not algo_id or data[0].get("sCode", "0") != "0":
        raise OKXError(f"set_algo_tp_sl failed: {data[0]}")
    logger.info("Attached algo TP=%s SL=%s → algoId=%s", tp_price, sl_price, algo_id)
    return algo_id


def set_algo_tp_sl_for_position(
    inst_id: str,
    side: str,
    tp_price: str,
    sl_price: str,
) -> str:
    """Set standalone OCO algo TP/SL for an existing position. Returns algoId."""
    pos_side = "long" if side == "buy" else "short"
    payload = {
        "instId": inst_id,
        "tdMode": "cross",
        "algoType": "oco",
        "posSide": pos_side,
        "tpTriggerPx": tp_price,
        "tpOrdPx": "-1",
        "slTriggerPx": sl_price,
        "slOrdPx": "-1",
    }
    data = _post("/trade/order-algo", payload)
    if not data:
        raise OKXError("set_algo_tp_sl_for_position returned empty response")
    algo_id = data[0].get("algoId", "")
    if not algo_id or data[0].get("sCode", "0") != "0":
        raise OKXError(f"set_algo_tp_sl_for_position failed: {data[0]}")
    logger.info("Standalone algo TP=%s SL=%s → algoId=%s", tp_price, sl_price, algo_id)
    return algo_id


def cancel_order(inst_id: str, ord_id: str) -> None:
    """Cancel a pending order by ordId."""
    data = _post("/trade/cancel-order", {"instId": inst_id, "ordId": ord_id})
    logger.info("Cancelled order %s", ord_id)
    return data


def cancel_algo(inst_id: str, algo_id: str) -> None:
    """Cancel a pending algo (TP/SL) order by algoId."""
    data = _post("/trade/cancel-algos", {
        "algoId": algo_id,
        "instId": inst_id,
    })
    logger.info("Cancelled algo %s", algo_id)
    return data


def close_position(inst_id: str, pos_side: str = "net") -> dict:
    """Close an open position via market order. Returns the response dict."""
    payload: dict[str, Any] = {
        "instId": inst_id,
        "mgnMode": "cross",
        "posSide": pos_side,
    }
    data = _post("/trade/close-position", payload)
    logger.info("Closed position for %s (posSide=%s): %s", inst_id, pos_side, data)
    return data[0] if data else {}


# ── Pending orders ────────────────────────────────────────────────────────────

def get_pending_orders(inst_id: str) -> list[dict]:
    """Return list of open (unfilled) orders for inst_id."""
    return _get("/trade/orders-pending", {"instType": "SWAP", "instId": inst_id}) or []


def get_order(inst_id: str, ord_id: str) -> dict | None:
    """Fetch a specific order by ordId."""
    data = _get("/trade/order", {"instId": inst_id, "ordId": ord_id})
    return data[0] if data else None


def get_algo_pending(inst_id: str) -> list[dict]:
    """Return pending algo (TP/SL) orders for inst_id."""
    return _get("/trade/orders-algo-pending", {
        "instType": "SWAP",
        "instId": inst_id,
        "algoType": "oco",
    }) or []


# ── Fill history (for reconciliation) ────────────────────────────────────────

def get_fills(inst_id: str, limit: int = 10) -> list[dict]:
    """Return recent trade fills for inst_id."""
    return _get("/trade/fills", {"instType": "SWAP", "instId": inst_id, "limit": str(limit)}) or []
