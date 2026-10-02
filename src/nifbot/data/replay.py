"""Replay adapter: serves recorded snapshots as if they were live.

The replay clock only moves forward. Each getter returns the latest record
whose ``received_at`` is at or before the replay clock, so code running on top
of it cannot see the future.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterator
from datetime import date, datetime

from nifbot.data.adapter import DataError
from nifbot.data.models import OptionChain, Quote, Snapshot


class ReplayAdapter:
    """:class:`~nifbot.data.adapter.BrokerAdapter` over recorded snapshots."""

    def __init__(self, snapshots: list[Snapshot]) -> None:
        if not snapshots:
            raise DataError("replay: no snapshots")
        self._snaps = sorted(snapshots, key=lambda s: s.received_at)
        self._times = [s.received_at for s in self._snaps]
        self._now = self._times[0]

    @property
    def now(self) -> datetime:
        return self._now

    def times(self) -> list[datetime]:
        return list(self._times)

    def set_time(self, moment: datetime) -> None:
        """Move the replay clock forward (never backward)."""
        if moment < self._now:
            raise ValueError("replay clock cannot move backwards")
        self._now = moment

    def steps(self) -> Iterator[datetime]:
        """Advance the clock through every recorded snapshot time."""
        for t in self._times:
            if t >= self._now:
                self.set_time(t)
                yield t

    def _latest(self) -> list[Snapshot]:
        return self._snaps[: bisect_right(self._times, self._now)]

    def _find_quote(self, attr: str) -> Quote:
        for snap in reversed(self._latest()):
            q = getattr(snap, attr)
            if isinstance(q, Quote):
                return q
        raise DataError(f"replay: no {attr} at {self._now.isoformat()}")

    def get_spot(self) -> Quote:
        return self._find_quote("spot")

    def get_futures(self) -> Quote:
        return self._find_quote("futures")

    def get_vix(self) -> Quote:
        return self._find_quote("vix")

    def get_expiries(self) -> list[date]:
        return sorted({s.chain.expiry for s in self._latest() if s.chain is not None})

    def get_option_chain(self, expiry: date) -> OptionChain:
        for snap in reversed(self._latest()):
            if snap.chain is not None and snap.chain.expiry == expiry:
                return snap.chain
        raise DataError(f"replay: no chain for {expiry} at {self._now.isoformat()}")
