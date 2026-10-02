"""Global cues from FRED (US Federal Reserve Bank of St. Louis): official and free.

FRED series are end-of-day and published with a lag, so every value carries its
own as-of date and the brief shows it. Nothing is interpolated or estimated.
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from datetime import date

from nifbot.net import Fetcher, FetchError

STALE_DAYS = 4  # covers a weekend plus one holiday

URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"

# label, FRED series id, unit ("pct" = show % change; "level" = show bp change)
SERIES: tuple[tuple[str, str, str], ...] = (
    ("S&P 500", "SP500", "pct"),
    ("Dow Jones", "DJIA", "pct"),
    ("Nasdaq Comp.", "NASDAQCOM", "pct"),
    ("Nikkei 225", "NIKKEI225", "pct"),
    ("Brent crude", "DCOILBRENTEU", "pct"),
    ("US 10Y yield", "DGS10", "level"),
    ("Dollar index (broad)", "DTWEXBGS", "pct"),
    ("USD/INR", "DEXINUS", "pct"),
)


@dataclass(frozen=True)
class Cue:
    label: str
    series: str
    as_of: date
    value: float
    prev: float
    unit: str

    @property
    def change(self) -> float:
        """% change, or change in basis points for yields."""
        if self.unit == "level":
            return (self.value - self.prev) * 100
        return (self.value / self.prev - 1) * 100 if self.prev else 0.0

    def is_stale(self, today: date, max_age_days: int = STALE_DAYS) -> bool:
        """Older than ``max_age_days`` calendar days (FRED lags for some series)."""
        return (today - self.as_of).days > max_age_days

    def render(self, today: date | None = None) -> str:
        stale = " STALE" if today is not None and self.is_stale(today) else ""
        if self.unit == "level":
            txt = f"{self.label}: {self.value:.2f}% ({self.change:+.0f} bp)"
        else:
            txt = f"{self.label}: {self.value:,.2f} ({self.change:+.2f}%)"
        return f"{txt} [{self.as_of:%d %b}{stale}]"


def parse_fred_csv(text: str) -> list[tuple[date, float]]:
    """Parse FRED's CSV (``observation_date``/``DATE`` + value; ``.`` = missing)."""
    reader = csv.reader(io.StringIO(text))
    try:
        next(reader)
    except StopIteration:
        return []
    out: list[tuple[date, float]] = []
    for row in reader:
        if len(row) < 2:
            continue
        try:
            d = date.fromisoformat(row[0].strip())
            v = float(row[1])
        except ValueError:
            continue
        if math.isfinite(v):
            out.append((d, v))
    return sorted(out)


def latest_cue(label: str, series: str, unit: str, text: str) -> Cue | None:
    obs = parse_fred_csv(text)
    if len(obs) < 2:
        return None
    (_, prev), (d, val) = obs[-2], obs[-1]
    return Cue(label, series, d, val, prev, unit)


def fetch_cues(fetcher: Fetcher) -> tuple[list[Cue], list[str]]:
    """All configured cues; failures are listed by label, never guessed."""
    cues: list[Cue] = []
    missing: list[str] = []
    for label, series, unit in SERIES:
        try:
            text = fetcher.get(URL.format(series=series)).decode("utf-8", errors="replace")
        except FetchError:
            missing.append(label)
            continue
        cue = latest_cue(label, series, unit, text)
        if cue is None:
            missing.append(label)
        else:
            cues.append(cue)
    return cues, missing
