"""Dhan HQ v2 market-data adapter (read-only).

Only market-quote, option-chain and chart endpoints are called. No order
endpoint exists in this module. Response field names follow Dhan's v2 docs and
official SDK; if Dhan changes them, parsing raises :class:`DataError` (which the
live engine treats as a feed health failure) and the raw response is kept by
the recorder so nothing is lost.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import httpx
from pydantic import ValidationError

from nifbot.config import DhanSettings
from nifbot.data.adapter import DataError
from nifbot.data.models import OptionChain, OptionQuote, Quote
from nifbot.timeutil import now_ist

log = logging.getLogger(__name__)

RawHook = Callable[[str, dict[str, Any], Any, datetime], None]

INDEX_SEGMENT = "IDX_I"
FNO_SEGMENT = "NSE_FNO"


def _num(value: Any) -> float | None:
    """Coerce an API number to float; ``None`` for missing/NaN/non-numeric."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _int(value: Any) -> int | None:
    num = _num(value)
    return None if num is None else int(num)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _pos(value: Any) -> float | None:
    """Positive price or ``None`` (Dhan sends 0 for 'no bid/ask')."""
    num = _num(value)
    return num if num is not None and num > 0 else None


class _Throttle:
    """Enforces a minimum interval between calls to one endpoint group."""

    def __init__(
        self, min_interval: float, clock: Callable[[], float], sleep: Callable[[float], None]
    ) -> None:
        self._min = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            remaining = self._min - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


class DhanClient:
    """HTTPS JSON client for Dhan v2 with throttling, retries and no secret leakage."""

    def __init__(
        self,
        client_id: str,
        access_token: str,
        cfg: DhanSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], datetime] = now_ist,
        on_raw: RawHook | None = None,
    ) -> None:
        if not client_id or not access_token or "replace-me" in (client_id, access_token):
            raise DataError("DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN are not set")
        self._client_id = client_id
        self._cfg = cfg
        self._sleep = sleep
        self._clock = clock
        self.on_raw = on_raw
        self._throttles = {
            "quote": _Throttle(cfg.quote_min_interval_seconds, monotonic, sleep),
            "chain": _Throttle(cfg.chain_min_interval_seconds, monotonic, sleep),
            "history": _Throttle(cfg.history_min_interval_seconds, monotonic, sleep),
        }
        self._http = httpx.Client(
            base_url=cfg.base_url,
            timeout=cfg.timeout_seconds,
            transport=transport,
            verify=True,
            headers={
                "access-token": access_token,
                "client-id": client_id,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        self._http.close()

    def post(self, endpoint: str, payload: dict[str, Any], group: str) -> tuple[Any, datetime]:
        """POST with throttling and retries. Returns (json, received_at)."""
        body = {**payload, "dhanClientId": self._client_id}
        delay = 1.0
        for attempt in range(self._cfg.max_retries + 1):
            self._throttles[group].wait()
            try:
                resp = self._http.post(endpoint, json=body)
            except httpx.HTTPError as exc:
                log.warning("dhan %s network error: %s", endpoint, type(exc).__name__)
                if attempt == self._cfg.max_retries:
                    raise DataError(f"{endpoint}: network error ({type(exc).__name__})") from None
                self._sleep(delay)
                delay *= 2
                continue
            received_at = self._clock()
            try:
                data = resp.json()
            except ValueError:
                data = None
            if self.on_raw is not None:
                self.on_raw(endpoint, payload, data, received_at)
            if 200 <= resp.status_code < 300 and data is not None:
                return data, received_at
            if (resp.status_code == 429 or resp.status_code >= 500) and (
                attempt < self._cfg.max_retries
            ):
                log.warning("dhan %s HTTP %s, retrying", endpoint, resp.status_code)
                self._sleep(delay)
                delay *= 2
                continue
            code = data.get("errorCode") if isinstance(data, dict) else None
            msg = str(data.get("errorMessage", ""))[:200] if isinstance(data, dict) else ""
            raise DataError(f"{endpoint}: HTTP {resp.status_code} {code or ''} {msg}".strip())
        raise DataError(f"{endpoint}: retries exhausted")  # pragma: no cover


def _unwrap(data: Any) -> Any:
    """Dhan wraps most payloads as ``{"data": ..., "status": "success"}``."""
    if isinstance(data, dict) and "data" in data:
        if data.get("status") not in (None, "success"):
            raise DataError(f"status={data.get('status')!r}")
        return data["data"]
    return data


def parse_market_quote(
    data: Any, segment: str, security_id: str, symbol: str, received_at: datetime
) -> Quote:
    """Parse one instrument out of a /marketfeed/{ltp,ohlc,quote} response."""
    try:
        item = _unwrap(data)[segment][str(security_id)]
    except (KeyError, TypeError) as exc:
        raise DataError(f"{symbol}: quote missing for {segment}/{security_id}") from exc
    if not isinstance(item, dict):
        raise DataError(f"{symbol}: malformed quote")
    ohlc = _dict(item.get("ohlc"))
    depth = _dict(item.get("depth"))
    buy = _dict((depth.get("buy") or [{}])[0])
    sell = _dict((depth.get("sell") or [{}])[0])
    try:
        return Quote(
            symbol=symbol,
            security_id=str(security_id),
            ltp=_num(item.get("last_price")) or 0.0,
            open=_pos(ohlc.get("open")),
            high=_pos(ohlc.get("high")),
            low=_pos(ohlc.get("low")),
            prev_close=_pos(ohlc.get("close")),
            volume=_int(item.get("volume")),
            oi=_int(item.get("oi")),
            avg_price=_pos(item.get("average_price")),
            bid=_pos(buy.get("price")),
            ask=_pos(sell.get("price")),
            received_at=received_at,
        )
    except ValidationError as exc:
        raise DataError(f"{symbol}: invalid quote ({exc.error_count()} errors)") from None


def _side(strike: float, option_type: str, raw: Any) -> OptionQuote | None:
    if not isinstance(raw, dict):
        return None
    greeks = _dict(raw.get("greeks"))
    return OptionQuote(
        strike=strike,
        option_type="CE" if option_type == "CE" else "PE",
        ltp=_num(raw.get("last_price")) or 0.0,
        bid=_pos(raw.get("top_bid_price")),
        ask=_pos(raw.get("top_ask_price")),
        bid_qty=_int(raw.get("top_bid_quantity")),
        ask_qty=_int(raw.get("top_ask_quantity")),
        oi=_int(raw.get("oi")) or 0,
        prev_oi=_int(raw.get("previous_oi")),
        volume=_int(raw.get("volume")) or 0,
        iv=_num(raw.get("implied_volatility")),
        delta=_num(greeks.get("delta")),
        gamma=_num(greeks.get("gamma")),
        theta=_num(greeks.get("theta")),
        vega=_num(greeks.get("vega")),
        security_id=str(raw["security_id"]) if raw.get("security_id") is not None else None,
    )


def parse_option_chain(
    data: Any, underlying: str, expiry: date, received_at: datetime
) -> OptionChain:
    """Parse a /optionchain response into an :class:`OptionChain`."""
    body = _unwrap(data)
    if not isinstance(body, dict) or not isinstance(body.get("oc"), dict):
        raise DataError("option chain: missing 'oc'")
    rows: list[OptionQuote] = []
    try:
        for strike_key, sides in body["oc"].items():
            strike = _num(strike_key)
            if strike is None or strike <= 0 or not isinstance(sides, dict):
                continue
            for key, otype in (("ce", "CE"), ("pe", "PE")):
                row = _side(strike, otype, sides.get(key))
                if row is not None:
                    rows.append(row)
        rows.sort(key=lambda r: (r.strike, r.option_type))
        return OptionChain(
            underlying=underlying,
            expiry=expiry,
            underlying_ltp=_num(body.get("last_price")) or 0.0,
            rows=tuple(rows),
            received_at=received_at,
        )
    except ValidationError as exc:
        raise DataError(f"option chain: invalid ({exc.error_count()} errors)") from None


def parse_expiries(data: Any) -> list[date]:
    """Parse /optionchain/expirylist into sorted dates."""
    body = _unwrap(data)
    if not isinstance(body, list):
        raise DataError("expiry list: not a list")
    out: set[date] = set()
    for item in body:
        try:
            out.add(date.fromisoformat(str(item)[:10]))
        except ValueError:
            continue
    if not out:
        raise DataError("expiry list: empty")
    return sorted(out)


class DhanAdapter:
    """:class:`~nifbot.data.adapter.BrokerAdapter` backed by Dhan v2 REST."""

    def __init__(self, client: DhanClient, cfg: DhanSettings, futures_security_id: str) -> None:
        self._c = client
        self._cfg = cfg
        self._fut_id = futures_security_id

    def _index_quote(self, security_id: str, symbol: str) -> Quote:
        payload = {INDEX_SEGMENT: [int(security_id)]}
        data, ts = self._c.post("/marketfeed/ohlc", payload, "quote")
        return parse_market_quote(data, INDEX_SEGMENT, security_id, symbol, ts)

    def get_spot(self) -> Quote:
        return self._index_quote(self._cfg.nifty_security_id, "NIFTY")

    def get_vix(self) -> Quote:
        return self._index_quote(self._cfg.vix_security_id, "INDIAVIX")

    def get_futures(self) -> Quote:
        payload = {FNO_SEGMENT: [int(self._fut_id)]}
        data, ts = self._c.post("/marketfeed/quote", payload, "quote")
        return parse_market_quote(data, FNO_SEGMENT, self._fut_id, "NIFTY-FUT", ts)

    def get_expiries(self) -> list[date]:
        payload = {"UnderlyingScrip": int(self._cfg.nifty_security_id), "UnderlyingSeg": "IDX_I"}
        data, _ = self._c.post("/optionchain/expirylist", payload, "chain")
        return parse_expiries(data)

    def get_option_chain(self, expiry: date) -> OptionChain:
        payload = {
            "UnderlyingScrip": int(self._cfg.nifty_security_id),
            "UnderlyingSeg": INDEX_SEGMENT,
            "Expiry": expiry.isoformat(),
        }
        data, ts = self._c.post("/optionchain", payload, "chain")
        return parse_option_chain(data, "NIFTY", expiry, ts)
