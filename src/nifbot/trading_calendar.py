"""NSE trading calendar: weekends, holidays and scheduled events.

The calendar refuses to guess. A year missing from ``holidays.yaml`` raises
:class:`CalendarError`, which the live engine treats as a kill-switch condition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from nifbot.config import CONFIG_DIR, load_yaml
from nifbot.timeutil import IST, parse_hhmm


class CalendarError(RuntimeError):
    """Raised when the calendar cannot answer reliably."""


class _Holiday(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: date
    name: str


class _Year(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verified: bool = False
    source: str = ""
    holidays: list[_Holiday]
    special_sessions: list[_Holiday] = []


class _HolidayFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    years: dict[int, _Year]


class MarketEvent(BaseModel):
    """A scheduled market-moving event."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    date: date
    time: str | None = None
    name: str
    impact: Literal["high", "medium", "low"]


class _EventFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[MarketEvent] | None = None


@dataclass(frozen=True)
class TradingCalendar:
    """Answers whether a date is a trading day and which events fall on it."""

    years: dict[int, _Year]
    events: tuple[MarketEvent, ...] = ()

    @classmethod
    def load(
        cls, holidays_path: Path | None = None, events_path: Path | None = None
    ) -> TradingCalendar:
        """Load holidays and events from YAML."""
        hol = _HolidayFile.model_validate(load_yaml(holidays_path or CONFIG_DIR / "holidays.yaml"))
        ev_path = events_path or CONFIG_DIR / "events.yaml"
        ev = _EventFile.model_validate(load_yaml(ev_path)) if ev_path.exists() else _EventFile()
        return cls(years=hol.years, events=tuple(ev.events or ()))

    def _year(self, day: date) -> _Year:
        year = self.years.get(day.year)
        if year is None:
            raise CalendarError(f"No NSE holiday list for {day.year} in holidays.yaml")
        return year

    def is_verified(self, day: date) -> bool:
        """Whether the holiday list for ``day``'s year was checked against NSE."""
        return self._year(day).verified

    def holiday_name(self, day: date) -> str | None:
        """Holiday name if ``day`` is an NSE holiday, else ``None``."""
        for holiday in self._year(day).holidays:
            if holiday.date == day:
                return holiday.name
        return None

    def is_special_session(self, day: date) -> bool:
        """True for special sessions such as Muhurat trading (no calls sent)."""
        return any(s.date == day for s in self._year(day).special_sessions)

    def is_trading_day(self, day: date) -> bool:
        """True for a regular NSE trading day (weekday, not a holiday)."""
        if day.weekday() >= 5:
            self._year(day)  # still fail loudly on an unknown year
            return False
        return self.holiday_name(day) is None

    def in_window(self, moment: datetime, start: str, end: str) -> bool:
        """True if ``moment`` (any tz) is a trading day and within [start, end] IST."""
        local = moment.astimezone(IST)
        if not self.is_trading_day(local.date()):
            return False
        t: time = local.time()
        return parse_hhmm(start) <= t <= parse_hhmm(end)

    def events_on(self, day: date) -> list[MarketEvent]:
        """Scheduled events on ``day``."""
        return [e for e in self.events if e.date == day]
