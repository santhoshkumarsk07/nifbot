"""Session recorder.

Each poll cycle appends one :class:`Snapshot` as a JSON line to
``<data_dir>/<YYYY-MM-DD>/snapshots.jsonl`` (crash-safe append) and every raw
broker response to ``raw.jsonl``. ``compact`` turns a day into Parquet tables.
"""

from __future__ import annotations

import gzip
import json
import logging
import shutil
import threading
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from nifbot.data.adapter import BrokerAdapter, DataError
from nifbot.data.models import OptionChain, Quote, Snapshot
from nifbot.timeutil import IST, now_ist, parse_hhmm
from nifbot.trading_calendar import TradingCalendar

log = logging.getLogger(__name__)


def day_dir(data_dir: Path, day: date) -> Path:
    return data_dir / day.isoformat()


class RawWriter:
    """Hook for :class:`~nifbot.data.dhan.DhanClient` that stores raw responses."""

    def __init__(self, data_dir: Path) -> None:
        self._dir = data_dir

    def __call__(self, endpoint: str, request: dict[str, Any], body: Any, at: datetime) -> None:
        path = day_dir(self._dir, at.astimezone(IST).date())
        path.mkdir(parents=True, exist_ok=True)
        line = {"received_at": at.isoformat(), "endpoint": endpoint, "request": request, "body": body}
        with (path / "raw.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, separators=(",", ":"), default=str) + "\n")


class Recorder:
    """Polls a :class:`BrokerAdapter` and stores snapshots."""

    def __init__(
        self,
        adapter: BrokerAdapter,
        data_dir: Path,
        clock: Callable[[], datetime] = now_ist,
    ) -> None:
        self._adapter = adapter
        self._dir = data_dir
        self._clock = clock
        self._expiry: date | None = None
        self._expiry_day: date | None = None

    def _current_expiry(self, today: date) -> date:
        if self._expiry_day != today or self._expiry is None:
            future = [e for e in self._adapter.get_expiries() if e >= today]
            if not future:
                raise DataError("no upcoming expiry")
            self._expiry, self._expiry_day = future[0], today
        return self._expiry

    def snapshot(self) -> Snapshot:
        """Fetch every source once. Failed parts are recorded as errors, never filled in."""
        started = self._clock()
        errors: list[str] = []

        def attempt(name: str, fn: Callable[[], Any]) -> Any:
            try:
                return fn()
            except DataError as exc:
                errors.append(f"{name}: {exc}")
                log.warning("snapshot %s failed: %s", name, exc)
                return None

        spot: Quote | None = attempt("spot", self._adapter.get_spot)
        futures: Quote | None = attempt("futures", self._adapter.get_futures)
        vix: Quote | None = attempt("vix", self._adapter.get_vix)
        expiry: date | None = attempt("expiry", lambda: self._current_expiry(started.date()))
        chain: OptionChain | None = None
        if expiry is not None:
            chain = attempt("chain", lambda: self._adapter.get_option_chain(expiry))
        return Snapshot(
            received_at=self._clock(),
            spot=spot,
            futures=futures,
            vix=vix,
            chain=chain,
            errors=tuple(errors),
        )

    def write(self, snap: Snapshot) -> Path:
        """Append a snapshot to its day's JSONL file."""
        path = day_dir(self._dir, snap.received_at.astimezone(IST).date())
        path.mkdir(parents=True, exist_ok=True)
        out = path / "snapshots.jsonl"
        with out.open("a", encoding="utf-8") as fh:
            fh.write(snap.model_dump_json() + "\n")
        return out

    def run(
        self,
        calendar: TradingCalendar,
        start: str,
        end: str,
        interval_seconds: int,
        stop: threading.Event,
        on_snapshot: Callable[[Snapshot], None] | None = None,
    ) -> int:
        """Record until ``end`` (IST) or ``stop`` is set. Returns snapshots written."""
        written = 0
        while not stop.is_set():
            now = self._clock()
            if not calendar.is_trading_day(now.date()):
                log.info("not a trading day: %s", now.date())
                return written
            end_t = datetime.combine(now.date(), parse_hhmm(end), tzinfo=IST)
            if now > end_t:
                return written
            if calendar.in_window(now, start, end):
                snap = self.snapshot()
                self.write(snap)
                written += 1
                if on_snapshot is not None:
                    on_snapshot(snap)
            # sleep to the next interval boundary
            now = self._clock()
            epoch = now.timestamp()
            nxt = (int(epoch // interval_seconds) + 1) * interval_seconds
            stop.wait(max(0.0, nxt - epoch))
        return written


def load_snapshots(path: Path) -> list[Snapshot]:
    """Load a day's snapshots (``snapshots.jsonl`` or ``.jsonl.gz``), skipping corrupt lines."""
    opener: Callable[..., Any] = gzip.open if path.suffix == ".gz" else open
    out: list[Snapshot] = []
    with opener(path, "rt", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                out.append(Snapshot.model_validate_json(line))
            except ValueError:
                log.warning("skipping corrupt snapshot line %d in %s", n, path)
    return sorted(out, key=lambda s: s.received_at)


def snapshots_to_frames(snaps: list[Snapshot]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Flatten snapshots into (quotes, chain) DataFrames."""
    quotes: list[dict[str, Any]] = []
    chain: list[dict[str, Any]] = []
    for s in snaps:
        for kind, q in (("spot", s.spot), ("futures", s.futures), ("vix", s.vix)):
            if q is not None:
                quotes.append({"kind": kind, **q.model_dump()})
        if s.chain is not None:
            for row in s.chain.rows:
                chain.append(
                    {
                        "received_at": s.chain.received_at,
                        "expiry": s.chain.expiry,
                        "underlying_ltp": s.chain.underlying_ltp,
                        **row.model_dump(),
                    }
                )
    return pd.DataFrame(quotes), pd.DataFrame(chain)


def compact(path: Path) -> list[Path]:
    """Convert a recorded day to Parquet and gzip the JSONL files."""
    src = path / "snapshots.jsonl"
    if not src.exists():
        raise FileNotFoundError(src)
    quotes, chain = snapshots_to_frames(load_snapshots(src))
    written: list[Path] = []
    for name, frame in (("quotes", quotes), ("chain", chain)):
        if not frame.empty:
            out = path / f"{name}.parquet"
            frame.to_parquet(out, index=False)
            written.append(out)
    for name in ("snapshots.jsonl", "raw.jsonl"):
        f = path / name
        if f.exists():
            with f.open("rb") as fin, gzip.open(f.with_suffix(".jsonl.gz"), "ab") as fout:
                shutil.copyfileobj(fin, fout)
            f.unlink()
            written.append(f.with_suffix(".jsonl.gz"))
    return written


def find_day_file(data_dir: Path, day: date) -> Path:
    """Path of a recorded day's snapshot file (plain or gzipped)."""
    base = day_dir(data_dir, day)
    for name in ("snapshots.jsonl", "snapshots.jsonl.gz"):
        if (base / name).exists():
            return base / name
    raise FileNotFoundError(f"no recording for {day} in {data_dir}")


__all__ = [
    "RawWriter",
    "Recorder",
    "compact",
    "find_day_file",
    "load_snapshots",
    "snapshots_to_frames",
]
