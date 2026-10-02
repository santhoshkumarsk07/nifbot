"""Resolve Nifty futures contracts (security id, expiry, lot size) from Dhan's scrip master."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date

import httpx

from nifbot.data.adapter import DataError

_ALIASES = {
    "exchange": ("SEM_EXM_EXCH_ID", "EXCH_ID"),
    "security_id": ("SEM_SMST_SECURITY_ID", "SECURITY_ID"),
    "instrument": ("SEM_INSTRUMENT_NAME", "INSTRUMENT"),
    "symbol": ("SEM_TRADING_SYMBOL", "SYMBOL_NAME"),
    "expiry": ("SEM_EXPIRY_DATE", "SM_EXPIRY_DATE"),
    "lot": ("SEM_LOT_UNITS", "LOT_SIZE"),
    "underlying": ("UNDERLYING_SYMBOL",),
}


@dataclass(frozen=True)
class FutureContract:
    """A Nifty index futures contract."""

    security_id: str
    symbol: str
    expiry: date
    lot_size: int


def _col(header: list[str], key: str) -> int | None:
    for name in _ALIASES[key]:
        if name in header:
            return header.index(name)
    return None


def nifty_futures(csv_text: str) -> list[FutureContract]:
    """All NSE NIFTY 50 index futures in the scrip master, sorted by expiry."""
    reader = csv.reader(io.StringIO(csv_text))
    try:
        header = [h.strip() for h in next(reader)]
    except StopIteration:
        raise DataError("scrip master: empty") from None
    idx = {k: _col(header, k) for k in _ALIASES}
    required = ("exchange", "security_id", "instrument", "symbol", "expiry", "lot")
    if any(idx[k] is None for k in required):
        raise DataError("scrip master: unexpected columns")

    def get(row: list[str], key: str) -> str:
        i = idx[key]
        return row[i].strip() if i is not None and i < len(row) else ""

    out: list[FutureContract] = []
    for row in reader:
        if get(row, "exchange") != "NSE" or get(row, "instrument") != "FUTIDX":
            continue
        symbol = get(row, "symbol").upper()
        underlying = get(row, "underlying").upper()
        if not (underlying == "NIFTY" or symbol.startswith("NIFTY-")):
            continue
        try:
            expiry = date.fromisoformat(get(row, "expiry")[:10])
            lot = int(float(get(row, "lot")))
        except ValueError:
            continue
        out.append(FutureContract(get(row, "security_id"), symbol, expiry, lot))
    return sorted(out, key=lambda c: c.expiry)


def current_future(contracts: list[FutureContract], today: date) -> FutureContract:
    """Nearest contract expiring on or after ``today``."""
    for contract in contracts:
        if contract.expiry >= today:
            return contract
    raise DataError("scrip master: no live NIFTY futures contract")


def download_scrip_master(url: str, timeout: float = 60.0) -> str:
    """Download the scrip master CSV over HTTPS."""
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise DataError(f"scrip master download failed ({type(exc).__name__})") from None
    return resp.text
