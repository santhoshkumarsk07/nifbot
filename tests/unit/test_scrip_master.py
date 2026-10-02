"""Scrip master parsing (synthetic CSV)."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from nifbot.data import scrip_master
from nifbot.data.adapter import DataError
from nifbot.data.scrip_master import current_future, nifty_futures

HEADER = (
    "SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,SEM_EXPIRY_CODE,"
    "SEM_TRADING_SYMBOL,SEM_LOT_UNITS,SEM_CUSTOM_SYMBOL,SEM_EXPIRY_DATE,SEM_STRIKE_PRICE,"
    "SEM_OPTION_TYPE"
)
CSV = (
    HEADER
    + """
NSE,D,111,FUTIDX,0,NIFTY-Nov2026-FUT,65.0,NIFTY NOV FUT,2026-11-24 14:30:00,-0.01,XX
NSE,D,110,FUTIDX,0,NIFTY-Oct2026-FUT,65.0,NIFTY OCT FUT,2026-10-27 14:30:00,-0.01,XX
NSE,D,200,FUTIDX,0,BANKNIFTY-Oct2026-FUT,30.0,BANKNIFTY,2026-10-27 14:30:00,-0.01,XX
NSE,D,300,OPTIDX,0,NIFTY-Oct2026-25000-CE,65.0,x,2026-10-06 14:30:00,25000,CE
BSE,D,400,FUTIDX,0,NIFTY-Oct2026-FUT,65.0,x,2026-10-27 14:30:00,-0.01,XX
NSE,D,500,FUTIDX,0,NIFTY-Dec2026-FUT,bad,x,2026-12-29 14:30:00,-0.01,XX
"""
)


def test_nifty_futures_and_current() -> None:
    futs = nifty_futures(CSV)
    assert [f.security_id for f in futs] == ["110", "111"]
    assert futs[0].lot_size == 65
    assert current_future(futs, date(2026, 10, 27)).security_id == "110"
    assert current_future(futs, date(2026, 10, 28)).security_id == "111"
    with pytest.raises(DataError):
        current_future(futs, date(2027, 1, 1))


def test_bad_csv() -> None:
    with pytest.raises(DataError):
        nifty_futures("")
    with pytest.raises(DataError):
        nifty_futures("A,B\n1,2\n")


def test_download(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **_: object) -> httpx.Response:
        return httpx.Response(200, text=CSV, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    assert scrip_master.download_scrip_master("https://x.test/a.csv") == CSV

    def fail(url: str, **_: object) -> httpx.Response:
        raise httpx.ConnectError("x")

    monkeypatch.setattr(httpx, "get", fail)
    with pytest.raises(DataError):
        scrip_master.download_scrip_master("https://x.test/a.csv")
