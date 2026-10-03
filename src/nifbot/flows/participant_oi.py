"""NSE participant-wise open interest (official daily archive CSV).

File: ``fao_participant_oi_DDMMYYYY.csv`` - a title line, then a header row and
rows for Client, DII, FII, Pro and TOTAL (number of contracts).
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date

from nifbot.net import Fetcher

URL = "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{d:%d%m%Y}.csv"
PARTICIPANTS = ("Client", "DII", "FII", "Pro")

_FIELDS = {
    "future index long": "fut_idx_long",
    "future index short": "fut_idx_short",
    "option index call long": "opt_idx_call_long",
    "option index put long": "opt_idx_put_long",
    "option index call short": "opt_idx_call_short",
    "option index put short": "opt_idx_put_short",
    "total long contracts": "total_long",
    "total short contracts": "total_short",
}


class ParticipantOIError(ValueError):
    pass


@dataclass(frozen=True)
class ParticipantOI:
    """Index derivatives positions of one participant category on one day."""

    day: date
    participant: str
    fut_idx_long: int
    fut_idx_short: int
    opt_idx_call_long: int
    opt_idx_put_long: int
    opt_idx_call_short: int
    opt_idx_put_short: int
    total_long: int
    total_short: int

    @property
    def fut_long_ratio(self) -> float:
        """Index futures long / (long + short), 0..1. Above 0.5 means net long."""
        tot = self.fut_idx_long + self.fut_idx_short
        return self.fut_idx_long / tot if tot else 0.5

    @property
    def fut_net(self) -> int:
        return self.fut_idx_long - self.fut_idx_short


def parse(text: str, day: date) -> dict[str, ParticipantOI]:
    """Parse the archive CSV into ``{participant: ParticipantOI}``."""
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    header_i = next(
        (i for i, r in enumerate(rows) if r and r[0].strip().lower() == "client type"), None
    )
    if header_i is None:
        raise ParticipantOIError("participant OI: header row not found")
    header = [h.strip().lower() for h in rows[header_i]]
    cols = {}
    for name, key in _FIELDS.items():
        if name not in header:
            raise ParticipantOIError(f"participant OI: missing column {name!r}")
        cols[key] = header.index(name)
    out: dict[str, ParticipantOI] = {}
    for r in rows[header_i + 1 :]:
        who = r[0].strip()
        if who not in PARTICIPANTS:
            continue
        try:
            vals = {k: int(float(r[i].strip().replace(",", ""))) for k, i in cols.items()}
        except (ValueError, IndexError) as exc:
            raise ParticipantOIError(f"participant OI: bad row for {who}") from exc
        if any(v < 0 for v in vals.values()):
            raise ParticipantOIError(f"participant OI: negative value for {who}")
        out[who] = ParticipantOI(day=day, participant=who, **vals)
    if "FII" not in out:
        raise ParticipantOIError("participant OI: FII row missing")
    return out


_FILE_RE = re.compile(r"fao_participant_oi_(\d{2})(\d{2})(\d{4})", re.IGNORECASE)


def day_from_filename(name: str) -> date | None:
    """Trading date from an NSE file name like ``fao_participant_oi_01102026.csv``."""
    m = _FILE_RE.search(name)
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def fetch(fetcher: Fetcher, day: date) -> dict[str, ParticipantOI]:
    """Download and parse the file for ``day`` (published after market close)."""
    return parse(fetcher.get(URL.format(d=day)).decode("utf-8", errors="replace"), day)
