"""NIFTY weekly expiry dates from dated rules (config/contracts.yaml) and the calendar."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from nifbot.config import CONFIG_DIR, load_yaml
from nifbot.trading_calendar import CalendarError, TradingCalendar

_WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4}


class _Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_: date = Field(alias="from")
    weekday: str = Field(pattern="^(MON|TUE|WED|THU|FRI)$")


class ExpiryRules:
    """Maps a trading day to the weekly expiry that is current on that day."""

    def __init__(self, rules: list[tuple[date, int]], calendar: TradingCalendar) -> None:
        self._rules = sorted(rules)
        self._cal = calendar

    @classmethod
    def load(cls, calendar: TradingCalendar, path: Path | None = None) -> ExpiryRules:
        raw = load_yaml(path or CONFIG_DIR / "contracts.yaml")
        rules = [_Rule.model_validate(r) for r in raw["weekly_expiry"]]
        return cls([(r.from_, _WEEKDAYS[r.weekday]) for r in rules], calendar)

    def _weekday_for(self, day: date) -> int:
        wd: int | None = None
        for start, weekday in self._rules:
            if day >= start:
                wd = weekday
        if wd is None:
            raise CalendarError(f"no expiry rule for {day}")
        return wd

    def expiry_on_or_after(self, day: date) -> date:
        """Nominal weekday on/after ``day``, shifted back over holidays; never before ``day``."""
        probe = day
        for _ in range(3):  # this week's expiry might already be shifted before `day`
            wd = self._weekday_for(probe)
            nominal = probe + timedelta(days=(wd - probe.weekday()) % 7)
            actual = nominal
            while not self._cal.is_trading_day(actual):
                actual -= timedelta(days=1)
            if actual >= day:
                return actual
            probe = nominal + timedelta(days=1)
        raise CalendarError(f"could not resolve expiry for {day}")  # pragma: no cover

    def days_to_expiry(self, day: date) -> int:
        """Trading days from ``day`` to its expiry (0 on expiry day)."""
        exp = self.expiry_on_or_after(day)
        n, d = 0, day
        while d < exp:
            d += timedelta(days=1)
            if self._cal.is_trading_day(d):
                n += 1
        return n
