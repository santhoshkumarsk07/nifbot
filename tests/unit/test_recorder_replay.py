"""Recorder -> files -> replay round trip, partial failures, no look-ahead in replay."""

from __future__ import annotations

import gzip
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd
import pytest

from nifbot.config import load_settings
from nifbot.data.adapter import DataError
from nifbot.data.dhan import DhanAdapter, DhanClient
from nifbot.data.recorder import (
    RawWriter,
    Recorder,
    compact,
    find_day_file,
    load_snapshots,
)
from nifbot.data.replay import ReplayAdapter
from nifbot.timeutil import IST
from nifbot.trading_calendar import TradingCalendar
from tests.dhan_fakes import EXPIRY, FUT_ID, FakeDhan

DAY = date(2026, 10, 5)  # a Monday, not a holiday


class Clock:
    def __init__(self, start: datetime) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t


def _setup(tmp_path: Path, clock: Clock, fake: FakeDhan) -> Recorder:
    cfg = load_settings().broker.dhan
    client = DhanClient(
        "1",
        "tok-for-tests",
        cfg,
        transport=httpx.MockTransport(fake.handler),
        sleep=lambda s: None,
        monotonic=lambda: 0.0,
        clock=clock,
        on_raw=RawWriter(tmp_path),
    )
    return Recorder(DhanAdapter(client, cfg, FUT_ID), tmp_path, clock=clock)


def _record_day(tmp_path: Path, minutes: int = 5) -> list[datetime]:
    clock = Clock(datetime(2026, 10, 5, 9, 15, tzinfo=IST))
    fake = FakeDhan()
    rec = _setup(tmp_path, clock, fake)
    times = []
    for i in range(minutes):
        fake.spot = 25000.0 + i * 10
        rec.write(rec.snapshot())
        times.append(clock.t)
        clock.t += timedelta(minutes=1)
    return times


def test_snapshot_round_trip_and_replay(tmp_path: Path) -> None:
    times = _record_day(tmp_path)
    path = find_day_file(tmp_path, DAY)
    snaps = load_snapshots(path)
    assert len(snaps) == 5 and not snaps[0].errors
    assert (tmp_path / DAY.isoformat() / "raw.jsonl").exists()

    replay = ReplayAdapter(snaps)
    seen = []
    for t in replay.steps():
        seen.append(replay.get_spot().ltp)
        assert replay.get_spot().received_at <= t
        assert replay.get_option_chain(EXPIRY).received_at <= t
    assert seen == [25000.0, 25010.0, 25020.0, 25030.0, 25040.0]
    assert replay.get_expiries() == [EXPIRY]
    assert replay.get_futures().oi == 15_000_000 and replay.get_vix().ltp == 13.2
    assert replay.times() == times


def test_replay_never_shows_future(tmp_path: Path) -> None:
    times = _record_day(tmp_path)
    replay = ReplayAdapter(load_snapshots(find_day_file(tmp_path, DAY)))
    replay.set_time(times[1] + timedelta(seconds=30))
    assert replay.get_spot().ltp == 25010.0  # not the 25020 recorded later
    with pytest.raises(ValueError):
        replay.set_time(times[0])
    with pytest.raises(DataError):
        ReplayAdapter([])


def test_replay_missing_parts_raise(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 10, 5, 9, 15, tzinfo=IST))
    fake = FakeDhan()
    fake.fail["/marketfeed/quote"] = [httpx.Response(400, json={"errorCode": "DH-905"})]
    fake.fail["/optionchain/expirylist"] = [httpx.Response(400, json={})]
    rec = _setup(tmp_path, clock, fake)
    snap = rec.snapshot()
    assert snap.futures is None and snap.chain is None and snap.spot is not None
    assert len(snap.errors) == 2
    replay = ReplayAdapter([snap])
    with pytest.raises(DataError):
        replay.get_futures()
    with pytest.raises(DataError):
        replay.get_option_chain(EXPIRY)


def test_no_upcoming_expiry(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 12, 1, 9, 15, tzinfo=IST))
    snap = _setup(tmp_path, clock, FakeDhan()).snapshot()
    assert snap.chain is None and any("no upcoming expiry" in e for e in snap.errors)


def test_compact_to_parquet(tmp_path: Path) -> None:
    _record_day(tmp_path, minutes=3)
    out = compact(tmp_path / DAY.isoformat())
    names = sorted(p.name for p in out)
    assert names == ["chain.parquet", "quotes.parquet", "raw.jsonl.gz", "snapshots.jsonl.gz"]
    chain = pd.read_parquet(tmp_path / DAY.isoformat() / "chain.parquet")
    assert len(chain) == 3 * 18 and {"strike", "oi", "iv", "received_at"} <= set(chain.columns)
    quotes = pd.read_parquet(tmp_path / DAY.isoformat() / "quotes.parquet")
    assert sorted(quotes["kind"].unique()) == ["futures", "spot", "vix"]
    gz = find_day_file(tmp_path, DAY)
    assert gz.suffix == ".gz" and len(load_snapshots(gz)) == 3
    with pytest.raises(FileNotFoundError):
        compact(tmp_path / "2026-10-06")
    with pytest.raises(FileNotFoundError):
        find_day_file(tmp_path, date(2026, 10, 6))


def test_corrupt_lines_skipped(tmp_path: Path) -> None:
    _record_day(tmp_path, minutes=2)
    path = find_day_file(tmp_path, DAY)
    with path.open("a") as fh:
        fh.write('{"truncated": \n\n')
    assert len(load_snapshots(path)) == 2
    with gzip.open(tmp_path / "x.jsonl.gz", "wt") as fh:
        fh.write(path.read_text())
    assert len(load_snapshots(tmp_path / "x.jsonl.gz")) == 2


def test_run_loop_respects_window_and_calendar(tmp_path: Path) -> None:
    cal = TradingCalendar.load()
    clock = Clock(datetime(2026, 10, 5, 15, 33, tzinfo=IST))
    rec = _setup(tmp_path, clock, FakeDhan())

    class StepEvent(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            clock.t += timedelta(seconds=timeout or 60)
            return False

    got: list[object] = []
    n = rec.run(cal, "09:00", "15:35", 60, StepEvent(), on_snapshot=got.append)
    assert n == 3 and len(got) == 3  # 15:33, 15:34, 15:35 (inclusive), then stops

    holiday = Clock(datetime(2026, 10, 2, 10, 0, tzinfo=IST))
    assert _setup(tmp_path, holiday, FakeDhan()).run(cal, "09:00", "15:35", 60, StepEvent()) == 0

    stopped = threading.Event()
    stopped.set()
    assert rec.run(cal, "09:00", "15:35", 60, stopped) == 0
