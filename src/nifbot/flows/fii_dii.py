"""FII/DII provisional cash-market flows.

NSE publishes these on its website (``/api/fiidiiTradeReact``). NSE's website
terms restrict automated access, so this connector is OFF by default
(``flows.fii_dii_enabled`` in settings). You can instead enter the figures with
``nifbot flows-add`` or switch it on if you accept NSE's terms.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime

URL = "https://www.nseindia.com/api/fiidiiTradeReact"


class FiiDiiError(ValueError):
    pass


@dataclass(frozen=True)
class CashFlow:
    """Net cash-market buying (₹ crore) by FIIs/FPIs and DIIs on one day."""

    day: date
    fii_net_cr: float
    dii_net_cr: float


def _num(v: object) -> float:
    return float(str(v).replace(",", "").strip())


def parse(data: bytes | str) -> CashFlow:
    """Parse NSE's JSON list ``[{category, date, buyValue, sellValue, netValue}, ...]``."""
    try:
        rows = json.loads(data)
    except ValueError as exc:
        raise FiiDiiError("fii/dii: not JSON") from exc
    if not isinstance(rows, list):
        raise FiiDiiError("fii/dii: expected a list")
    fii = dii = None
    day: date | None = None
    for r in rows:
        if not isinstance(r, dict):
            continue
        cat = str(r.get("category", "")).upper()
        try:
            net = _num(r.get("netValue"))
            day = datetime.strptime(str(r.get("date", "")).strip(), "%d-%b-%Y").date()
        except ValueError:
            continue
        if cat.startswith("FII") or cat.startswith("FPI"):
            fii = net
        elif cat.startswith("DII"):
            dii = net
    if fii is None or dii is None or day is None:
        raise FiiDiiError("fii/dii: FII or DII row missing")
    return CashFlow(day, fii, dii)


def trend(flows: list[CashFlow], days: int) -> tuple[float, float] | None:
    """Sum of FII and DII net flows over the last ``days`` entries."""
    recent = sorted(flows, key=lambda f: f.day)[-days:]
    if len(recent) < days:
        return None
    return sum(f.fii_net_cr for f in recent), sum(f.dii_net_cr for f in recent)
