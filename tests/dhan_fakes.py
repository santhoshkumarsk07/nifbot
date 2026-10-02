"""SYNTHETIC Dhan v2 responses for tests. Not real market data."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx

EXPIRY = date(2026, 10, 6)
FUT_ID = "52000"


def ohlc_body(security_id: str, ltp: float) -> dict[str, Any]:
    return {
        "data": {
            "IDX_I": {
                security_id: {
                    "last_price": ltp,
                    "ohlc": {"open": ltp - 10, "high": ltp + 20, "low": ltp - 30, "close": ltp - 5},
                }
            }
        },
        "status": "success",
    }


def quote_body(ltp: float, oi: int) -> dict[str, Any]:
    return {
        "data": {
            "NSE_FNO": {
                FUT_ID: {
                    "last_price": ltp,
                    "oi": oi,
                    "volume": 123456,
                    "average_price": ltp - 2,
                    "ohlc": {"open": ltp, "high": ltp + 1, "low": ltp - 1, "close": ltp},
                    "depth": {
                        "buy": [{"price": ltp - 0.5, "quantity": 75}],
                        "sell": [{"price": ltp + 0.5, "quantity": 75}],
                    },
                }
            }
        },
        "status": "success",
    }


def chain_body(spot: float, strikes: range = range(24800, 25250, 50)) -> dict[str, Any]:
    oc: dict[str, Any] = {}
    for k in strikes:
        dist = (k - spot) / 50
        oc[f"{k:.6f}"] = {
            "ce": {
                "last_price": max(1.0, 100 - dist * 20),
                "oi": 100000 + k,
                "previous_oi": 90000 + k,
                "volume": 5000,
                "implied_volatility": 12.5,
                "top_bid_price": max(0.95, 99 - dist * 20),
                "top_ask_price": max(1.05, 101 - dist * 20),
                "top_bid_quantity": 750,
                "top_ask_quantity": 600,
                "greeks": {"delta": 0.5, "gamma": 0.001, "theta": -10, "vega": 12},
                "security_id": 40000 + k,
            },
            "pe": {
                "last_price": max(1.0, 100 + dist * 20),
                "oi": 120000 + k,
                "previous_oi": 0,
                "volume": 4000,
                "implied_volatility": 13.0,
                "top_bid_price": 0,
                "top_ask_price": 0,
                "greeks": {"delta": -0.5},
            },
        }
    return {"data": {"last_price": spot, "oc": oc}, "status": "success"}


class FakeDhan:
    """Routes requests by path to scripted bodies; records requests."""

    def __init__(self, spot: float = 25000.0) -> None:
        self.spot = spot
        self.requests: list[tuple[str, dict[str, Any], httpx.Headers]] = []
        self.fail: dict[str, list[httpx.Response]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("/v2", "")
        body = json.loads(request.content or b"{}")
        self.requests.append((path, body, request.headers))
        if self.fail.get(path):
            return self.fail[path].pop(0)
        if path == "/marketfeed/ohlc":
            sid = str(body["IDX_I"][0])
            return httpx.Response(200, json=ohlc_body(sid, self.spot if sid == "13" else 13.2))
        if path == "/marketfeed/quote":
            return httpx.Response(200, json=quote_body(self.spot + 40, 15_000_000))
        if path == "/optionchain/expirylist":
            return httpx.Response(
                200,
                json={"data": ["2026-10-13", EXPIRY.isoformat(), "garbage"], "status": "success"},
            )
        if path == "/optionchain":
            return httpx.Response(200, json=chain_body(self.spot))
        return httpx.Response(404, json={"errorCode": "DH-404", "errorMessage": "unknown"})
