"""IST time helpers. All timestamps in the system are timezone-aware IST."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    """Return the current wall-clock time in IST."""
    return datetime.now(tz=IST)


def parse_hhmm(value: str) -> time:
    """Parse an ``HH:MM`` string into a :class:`datetime.time`."""
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))
