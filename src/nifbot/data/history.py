"""Historical candles from Dhan: index/futures intraday and expired options (rolling).

Candle timestamps mark the *start* of a bar, so each row also gets
``available_at = start + interval``. Features and backtests must only use a bar
once ``available_at`` has passed (this is what prevents look-ahead).
"""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from nifbot.data.adapter import DataError
from nifbot.data.dhan import DhanClient, _unwrap
from nifbot.timeutil import IST

log = logging.getLogger(__name__)

INTERVALS = (1, 5, 15, 25, 60)
ROLLING_FIELDS = ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"]


def date_chunks(start: date, end: date, days: int) -> Iterator[tuple[date, date]]:
    """Split [start, end] into inclusive windows of at most ``days`` days."""
    if end < start:
        raise ValueError("end before start")
    cur = start
    while cur <= end:
        nxt = min(end, cur + timedelta(days=days - 1))
        yield cur, nxt
        cur = nxt + timedelta(days=1)


def arrays_to_frame(body: Any, interval: int) -> pd.DataFrame:
    """Turn Dhan's column arrays (``open``, ``close``, ``timestamp``...) into a frame."""
    if not isinstance(body, dict) or not isinstance(body.get("timestamp"), list):
        raise DataError("history: missing timestamp array")
    n = len(body["timestamp"])
    cols: dict[str, list[Any]] = {}
    for key, values in body.items():
        if isinstance(values, list) and len(values) == n:
            cols["oi" if key == "open_interest" else key] = values
    frame = pd.DataFrame(cols)
    if frame.empty:
        return frame
    start = pd.to_datetime(frame.pop("timestamp"), unit="s", utc=True).dt.tz_convert(IST)
    frame.insert(0, "start", start)
    frame.insert(1, "available_at", start + pd.Timedelta(minutes=interval))
    return frame.drop_duplicates("start").sort_values("start").reset_index(drop=True)


@dataclass(frozen=True)
class RollingSpec:
    """One expired-options series request (strike is relative to ATM)."""

    expiry_flag: Literal["WEEK", "MONTH"]
    expiry_code: int
    strike: str  # "ATM", "ATM+1", "ATM-3", ...
    option_type: Literal["CALL", "PUT"]


def relative_strikes(width: int) -> list[str]:
    """``["ATM-width", ..., "ATM", ..., "ATM+width"]``."""
    return [f"ATM{i:+d}" if i else "ATM" for i in range(-width, width + 1)]


class DhanHistory:
    """Downloads history and stores Parquet plus raw JSON (gzipped)."""

    def __init__(self, client: DhanClient, out_dir: Path) -> None:
        self._c = client
        self._out = out_dir

    def _save_raw(self, name: str, request: dict[str, Any], body: Any) -> None:
        raw_dir = self._out / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        with gzip.open(raw_dir / f"{name}.json.gz", "wt", encoding="utf-8") as fh:
            json.dump({"request": request, "body": body}, fh, default=str)

    def intraday(
        self,
        security_id: str,
        segment: str,
        instrument: str,
        start: date,
        end: date,
        interval: int = 1,
        oi: bool = True,
        chunk_days: int = 90,
    ) -> pd.DataFrame:
        """1-minute (or other interval) candles for an index/future/stock."""
        if interval not in INTERVALS:
            raise ValueError(f"interval must be one of {INTERVALS}")
        frames: list[pd.DataFrame] = []
        for a, b in date_chunks(start, end, chunk_days):
            req = {
                "securityId": security_id,
                "exchangeSegment": segment,
                "instrument": instrument,
                "interval": interval,
                "oi": oi,
                "fromDate": f"{a.isoformat()} 09:00:00",
                "toDate": f"{b.isoformat()} 15:35:00",
            }
            data, _ = self._c.post("/charts/intraday", req, "history")
            self._save_raw(f"intraday_{security_id}_{a}_{b}_{interval}m", req, data)
            frame = arrays_to_frame(_unwrap(data), interval)
            log.info("intraday %s %s..%s: %d rows", security_id, a, b, len(frame))
            frames.append(frame)
        return _concat(frames, "start")

    def rolling_option(
        self,
        underlying_id: str,
        spec: RollingSpec,
        start: date,
        end: date,
        interval: int = 1,
        chunk_days: int = 30,
    ) -> pd.DataFrame:
        """Expired-options candles for one relative strike/side (Dhan /charts/rollingoption)."""
        frames: list[pd.DataFrame] = []
        side = "ce" if spec.option_type == "CALL" else "pe"
        for a, b in date_chunks(start, end, chunk_days):
            req = {
                "securityId": underlying_id,
                "exchangeSegment": "NSE_FNO",
                "instrument": "OPTIDX",
                "expiryFlag": spec.expiry_flag,
                "expiryCode": spec.expiry_code,
                "strike": spec.strike,
                "drvOptionType": spec.option_type,
                "requiredData": ROLLING_FIELDS,
                "fromDate": a.isoformat(),
                "toDate": b.isoformat(),
                "interval": interval,
            }
            try:
                data, _ = self._c.post("/charts/rollingoption", req, "history")
            except DataError as exc:
                log.warning("rolling %s %s..%s failed: %s", spec, a, b, exc)
                continue
            name = f"rolling_{spec.expiry_flag}{spec.expiry_code}_{spec.strike}_{side}_{a}_{b}"
            self._save_raw(name, req, data)
            body = _unwrap(data)
            series = body.get(side) if isinstance(body, dict) else None
            if not series:
                continue
            frame = arrays_to_frame(series, interval)
            frame["relative_strike"] = spec.strike
            frame["option_type"] = "CE" if side == "ce" else "PE"
            frame["expiry_flag"] = spec.expiry_flag
            frame["expiry_code"] = spec.expiry_code
            frames.append(frame)
        return _concat(frames, "start")

    def save(self, frame: pd.DataFrame, name: str) -> Path | None:
        """Write a frame to ``<out>/<name>.parquet`` (skips empty frames)."""
        if frame.empty:
            return None
        self._out.mkdir(parents=True, exist_ok=True)
        path = self._out / f"{name}.parquet"
        frame.to_parquet(path, index=False)
        return path


def _concat(frames: list[pd.DataFrame], key: str) -> pd.DataFrame:
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames, ignore_index=True).drop_duplicates(key)
    return merged.sort_values(key).reset_index(drop=True)


__all__ = [
    "DhanHistory",
    "RollingSpec",
    "arrays_to_frame",
    "date_chunks",
    "relative_strikes",
]
