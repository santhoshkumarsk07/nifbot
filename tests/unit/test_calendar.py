"""NSE calendar: weekends, holidays, unknown years, windows, events."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from nifbot.timeutil import IST
from nifbot.trading_calendar import CalendarError, TradingCalendar


@pytest.fixture
def cal(tmp_path: Path) -> TradingCalendar:
    hol = tmp_path / "h.yaml"
    hol.write_text(
        "years:\n  2026:\n    verified: true\n    holidays:\n"
        "      - {date: 2026-10-02, name: Gandhi Jayanti}\n"
        "    special_sessions:\n      - {date: 2026-11-08, name: Muhurat}\n"
    )
    ev = tmp_path / "e.yaml"
    ev.write_text("events:\n  - {date: 2026-10-06, name: RBI policy, impact: high}\n")
    return TradingCalendar.load(hol, ev)


def test_shipped_config_loads() -> None:
    cal = TradingCalendar.load()
    assert cal.holiday_name(date(2026, 1, 26)) == "Republic Day"


def test_weekday_is_trading(cal: TradingCalendar) -> None:
    assert cal.is_trading_day(date(2026, 10, 5))


def test_weekend_is_closed(cal: TradingCalendar) -> None:
    assert not cal.is_trading_day(date(2026, 10, 3))


def test_holiday_is_closed(cal: TradingCalendar) -> None:
    assert not cal.is_trading_day(date(2026, 10, 2))
    assert cal.holiday_name(date(2026, 10, 2)) == "Gandhi Jayanti"


def test_unknown_year_fails_loudly(cal: TradingCalendar) -> None:
    with pytest.raises(CalendarError):
        cal.is_trading_day(date(2027, 1, 4))
    with pytest.raises(CalendarError):
        cal.is_trading_day(date(2027, 1, 2))  # even a weekend


def test_window_uses_ist(cal: TradingCalendar) -> None:
    utc = ZoneInfo("UTC")
    assert cal.in_window(datetime(2026, 10, 5, 4, 0, tzinfo=utc), "09:00", "15:35")  # 09:30 IST
    assert not cal.in_window(datetime(2026, 10, 5, 10, 30, tzinfo=utc), "09:00", "15:35")
    assert not cal.in_window(datetime(2026, 10, 2, 10, 0, tzinfo=IST), "09:00", "15:35")


def test_events_and_special_sessions(cal: TradingCalendar) -> None:
    assert [e.name for e in cal.events_on(date(2026, 10, 6))] == ["RBI policy"]
    assert cal.events_on(date(2026, 10, 7)) == []
    assert cal.is_special_session(date(2026, 11, 8))
    assert cal.is_verified(date(2026, 1, 1))
