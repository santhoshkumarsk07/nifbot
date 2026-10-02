"""Dhan adapter parsing, HTTP behaviour and secrecy (synthetic responses)."""

from __future__ import annotations

import logging
from datetime import datetime

import httpx
import pytest

from nifbot.config import DhanSettings, load_settings
from nifbot.data.adapter import BrokerAdapter, DataError
from nifbot.data.dhan import (
    DhanAdapter,
    DhanClient,
    parse_expiries,
    parse_market_quote,
    parse_option_chain,
)
from nifbot.timeutil import IST
from tests.dhan_fakes import EXPIRY, FUT_ID, FakeDhan, chain_body, ohlc_body

TS = datetime(2026, 10, 5, 10, 0, tzinfo=IST)
TOKEN = "eyJhbGciOiJIUzUxMiJ9.eyJzZWNyZXQiOiJ0ZXN0In0.fake-signature-for-tests"


def _cfg() -> DhanSettings:
    return load_settings().broker.dhan


def _client(fake: FakeDhan, sleeps: list[float] | None = None) -> DhanClient:
    sl = sleeps if sleeps is not None else []
    return DhanClient(
        "1000000001",
        TOKEN,
        _cfg(),
        transport=httpx.MockTransport(fake.handler),
        sleep=sl.append,
        monotonic=lambda: 0.0,
        clock=lambda: TS,
    )


def test_parse_market_quote() -> None:
    q = parse_market_quote(ohlc_body("13", 25000.0), "IDX_I", "13", "NIFTY", TS)
    assert q.ltp == 25000.0 and q.prev_close == 24995.0 and q.received_at == TS
    with pytest.raises(DataError):
        parse_market_quote(ohlc_body("13", 25000.0), "IDX_I", "21", "VIX", TS)
    with pytest.raises(DataError):
        parse_market_quote({"data": {"IDX_I": {"13": {"last_price": -1}}}}, "IDX_I", "13", "N", TS)
    with pytest.raises(DataError):
        parse_market_quote({"data": {"IDX_I": {"13": "x"}}}, "IDX_I", "13", "N", TS)
    with pytest.raises(DataError):
        parse_market_quote({"status": "failure", "data": {}}, "IDX_I", "13", "N", TS)


def test_parse_option_chain() -> None:
    chain = parse_option_chain(chain_body(25000.0), "NIFTY", EXPIRY, TS)
    assert chain.underlying_ltp == 25000.0
    assert len(chain.rows) == 18
    ce = next(r for r in chain.rows if r.strike == 25000 and r.option_type == "CE")
    assert ce.change_in_oi == 10000 and ce.bid == 99 and ce.iv == 12.5 and ce.delta == 0.5
    pe = next(r for r in chain.rows if r.strike == 25000 and r.option_type == "PE")
    assert pe.bid is None and pe.ask is None  # 0 means no quote, not a price
    assert chain.rows == tuple(sorted(chain.rows, key=lambda r: (r.strike, r.option_type)))


def test_parse_option_chain_rejects_garbage() -> None:
    with pytest.raises(DataError):
        parse_option_chain({"data": {"last_price": 1}}, "NIFTY", EXPIRY, TS)
    with pytest.raises(DataError):
        parse_option_chain({"data": {"last_price": 0, "oc": {}}}, "NIFTY", EXPIRY, TS)
    bad = chain_body(25000.0)
    bad["data"]["oc"]["abc"] = {"ce": {}}
    bad["data"]["oc"]["25000.000000"]["ce"]["oi"] = -5
    with pytest.raises(DataError):
        parse_option_chain(bad, "NIFTY", EXPIRY, TS)


def test_parse_expiries() -> None:
    assert parse_expiries({"data": ["2026-10-13", "2026-10-06", "x"]})[0] == EXPIRY
    with pytest.raises(DataError):
        parse_expiries({"data": "nope"})
    with pytest.raises(DataError):
        parse_expiries({"data": []})


def test_adapter_end_to_end() -> None:
    fake = FakeDhan()
    adapter = DhanAdapter(_client(fake), _cfg(), FUT_ID)
    assert isinstance(adapter, BrokerAdapter)
    assert adapter.get_spot().ltp == 25000.0
    assert adapter.get_vix().ltp == 13.2
    fut = adapter.get_futures()
    assert fut.oi == 15_000_000 and fut.bid == 25039.5 and fut.ask == 25040.5
    assert adapter.get_expiries()[0] == EXPIRY
    assert len(adapter.get_option_chain(EXPIRY).rows) == 18
    _path, body, headers = fake.requests[0]
    assert headers["access-token"] == TOKEN and headers["client-id"] == "1000000001"
    assert body["dhanClientId"] == "1000000001"
    chain_req = next(b for p, b, _ in fake.requests if p == "/optionchain")
    assert chain_req["Expiry"] == "2026-10-06" and chain_req["UnderlyingSeg"] == "IDX_I"


def test_retry_then_success_and_raw_hook() -> None:
    fake = FakeDhan()
    fake.fail["/marketfeed/ohlc"] = [httpx.Response(429, json={}), httpx.Response(503)]
    sleeps: list[float] = []
    client = _client(fake, sleeps)
    raw: list[str] = []
    client.on_raw = lambda ep, req, body, at: raw.append(ep)
    assert DhanAdapter(client, _cfg(), FUT_ID).get_spot().ltp == 25000.0
    assert sleeps[:1] == [1.0] and 2.0 in sleeps
    assert raw == ["/marketfeed/ohlc"] * 3


def test_errors_never_contain_token(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeDhan()
    fake.fail["/marketfeed/ohlc"] = [
        httpx.Response(401, json={"errorCode": "DH-901", "errorMessage": "Invalid token"})
    ]
    with pytest.raises(DataError, match="DH-901") as info:
        DhanAdapter(_client(fake), _cfg(), FUT_ID).get_spot()
    assert TOKEN not in str(info.value)

    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"token {TOKEN}")

    client = DhanClient(
        "1", TOKEN, _cfg(), transport=httpx.MockTransport(boom), sleep=lambda s: None
    )
    with caplog.at_level(logging.DEBUG), pytest.raises(DataError) as info2:
        client.post("/marketfeed/ltp", {}, "quote")
    assert TOKEN not in str(info2.value)
    assert all(TOKEN not in r.getMessage() for r in caplog.records)
    client.close()


def test_throttle_enforces_min_interval() -> None:
    fake = FakeDhan()
    sleeps: list[float] = []
    t = [0.0]

    def fake_sleep(s: float) -> None:
        sleeps.append(s)
        t[0] += s

    client = DhanClient(
        "1",
        TOKEN,
        _cfg(),
        transport=httpx.MockTransport(fake.handler),
        sleep=fake_sleep,
        monotonic=lambda: t[0],
        clock=lambda: TS,
    )
    adapter = DhanAdapter(client, _cfg(), FUT_ID)
    adapter.get_option_chain(EXPIRY)
    adapter.get_option_chain(EXPIRY)
    assert sleeps == [3.0]


def test_missing_credentials_and_http_only() -> None:
    with pytest.raises(DataError):
        DhanClient("", TOKEN, _cfg())
    with pytest.raises(DataError):
        DhanClient("replace-me", TOKEN, _cfg())
    with pytest.raises(ValueError):
        DhanSettings.model_validate({**_cfg().model_dump(), "base_url": "http://api.dhan.co"})
