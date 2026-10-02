"""Historical downloader: chunking, available_at, parsing (synthetic responses)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import pytest

from nifbot.config import load_settings
from nifbot.data.adapter import DataError
from nifbot.data.dhan import DhanClient
from nifbot.data.history import (
    DhanHistory,
    RollingSpec,
    arrays_to_frame,
    date_chunks,
    relative_strikes,
)

T0 = 1791171900  # 2026-10-05 09:15:00 IST


def test_date_chunks() -> None:
    chunks = list(date_chunks(date(2026, 1, 1), date(2026, 3, 31), 30))
    assert chunks[0] == (date(2026, 1, 1), date(2026, 1, 30))
    assert chunks[-1][1] == date(2026, 3, 31)
    assert all(b >= a for a, b in chunks)
    with pytest.raises(ValueError):
        list(date_chunks(date(2026, 2, 1), date(2026, 1, 1), 5))


def test_arrays_to_frame_sets_available_at() -> None:
    body = {"timestamp": [T0 + 60, T0, T0], "close": [2, 1, 1], "open_interest": [5, 4, 4]}
    f = arrays_to_frame(body, 1)
    assert list(f["close"]) == [1, 2] and "oi" in f.columns
    assert str(f["start"].iloc[0]) == "2026-10-05 09:15:00+05:30"
    assert (f["available_at"] - f["start"]).iloc[0] == pd.Timedelta(minutes=1)
    with pytest.raises(DataError):
        arrays_to_frame({"close": [1]}, 1)
    assert arrays_to_frame({"timestamp": []}, 1).empty


def test_relative_strikes() -> None:
    assert relative_strikes(2) == ["ATM-2", "ATM-1", "ATM", "ATM+1", "ATM+2"]


def _client(handler: Any) -> DhanClient:
    return DhanClient(
        "1",
        "tok-tests",
        load_settings().broker.dhan,
        transport=httpx.MockTransport(handler),
        sleep=lambda s: None,
        monotonic=lambda: 0.0,
    )


def test_intraday_and_rolling(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body)
        if req.url.path.endswith("/charts/intraday"):
            return httpx.Response(
                200,
                json={
                    "timestamp": [T0, T0 + 60],
                    "open": [1, 2],
                    "close": [1, 2],
                    "open_interest": [0, 0],
                },
            )
        if body["fromDate"] == "2026-09-01":
            return httpx.Response(500)  # chunk fails -> skipped, not invented
        side = {
            "timestamp": [T0],
            "close": [100.0],
            "iv": [12.0],
            "strike": [25000],
            "spot": [25010.0],
            "oi": [1000],
        }
        return httpx.Response(200, json={"data": {"ce": side, "pe": None}})

    hist = DhanHistory(_client(handler), tmp_path)
    spot = hist.intraday("13", "IDX_I", "INDEX", date(2026, 7, 1), date(2026, 10, 5))
    assert len(spot) == 2  # two chunks, duplicates dropped
    assert seen[0]["fromDate"] == "2026-07-01 09:00:00" and seen[0]["interval"] == 1
    assert hist.save(spot, "spot") == tmp_path / "spot.parquet"
    assert hist.save(pd.DataFrame(), "empty") is None
    assert any((tmp_path / "raw").iterdir())
    with pytest.raises(ValueError):
        hist.intraday("13", "IDX_I", "INDEX", date(2026, 1, 1), date(2026, 1, 2), interval=2)

    ce = hist.rolling_option(
        "13", RollingSpec("WEEK", 1, "ATM+1", "CALL"), date(2026, 8, 1), date(2026, 9, 29)
    )
    assert len(ce) == 1 and ce["option_type"].iloc[0] == "CE"
    assert ce["relative_strike"].iloc[0] == "ATM+1"
    pe = hist.rolling_option(
        "13", RollingSpec("WEEK", 1, "ATM", "PUT"), date(2026, 8, 1), date(2026, 8, 10)
    )
    assert pe.empty
