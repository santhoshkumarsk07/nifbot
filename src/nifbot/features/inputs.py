"""Builders that turn recordings / history / flows into feature inputs.

Bars:  DatetimeIndex = available_at (IST); columns spot (required), fut, fut_oi,
       fut_vwap, vix.
Chain: columns available_at, strike, option_type, ltp, oi, volume, iv,
       underlying, prev_oi (optional).
Daily: index = trading date; only facts known BEFORE that day's open.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from nifbot.data.models import Snapshot
from nifbot.features.expiry import ExpiryRules
from nifbot.flows.store import FlowStore
from nifbot.trading_calendar import CalendarError, TradingCalendar

log = logging.getLogger(__name__)

CHAIN_COLUMNS = [
    "available_at",
    "strike",
    "option_type",
    "ltp",
    "oi",
    "volume",
    "iv",
    "underlying",
    "prev_oi",
]


def bars_from_snapshots(snaps: Sequence[Snapshot]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for s in snaps:
        if s.spot is None:
            continue
        rows.append(
            {
                "available_at": s.received_at,
                "spot": s.spot.ltp,
                "fut": s.futures.ltp if s.futures else np.nan,
                "fut_oi": s.futures.oi if s.futures and s.futures.oi is not None else np.nan,
                "fut_vwap": s.futures.avg_price if s.futures and s.futures.avg_price else np.nan,
                "vix": s.vix.ltp if s.vix else np.nan,
            }
        )
    if not rows:
        return pd.DataFrame(columns=["spot"])
    return pd.DataFrame(rows).set_index("available_at").sort_index()


def chain_from_snapshots(snaps: Sequence[Snapshot]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for s in snaps:
        c = s.chain
        if c is None:
            continue
        for r in c.rows:
            rows.append(
                {
                    "available_at": c.received_at,
                    "strike": r.strike,
                    "option_type": r.option_type,
                    "ltp": r.ltp,
                    "oi": r.oi,
                    "volume": r.volume,
                    "iv": r.iv if r.iv is not None else np.nan,
                    "underlying": c.underlying_ltp,
                    "prev_oi": r.prev_oi if r.prev_oi is not None else np.nan,
                }
            )
    return pd.DataFrame(rows, columns=CHAIN_COLUMNS)


def _by_available(frame: pd.DataFrame, cols: dict[str, str]) -> pd.DataFrame:
    out = frame.set_index("available_at")[list(cols)].rename(columns=cols)
    return out[~out.index.duplicated(keep="last")].sort_index()


def bars_from_history(
    spot: pd.DataFrame, vix: pd.DataFrame | None = None, fut: pd.DataFrame | None = None
) -> pd.DataFrame:
    """Combine Dhan candle frames (columns start, available_at, close, ...)."""
    bars = _by_available(spot, {"close": "spot"})
    if vix is not None and not vix.empty:
        bars = bars.join(_by_available(vix, {"close": "vix"}), how="left")
    if fut is not None and not fut.empty:
        f = fut.sort_values("available_at").copy()
        typ = (f["high"] + f["low"] + f["close"]) / 3
        day = f["available_at"].dt.date
        vol = f["volume"].astype(float)
        f["vwap"] = (typ * vol).groupby(day).cumsum() / vol.groupby(day).cumsum()
        cols = {"close": "fut", "vwap": "fut_vwap"}
        if "oi" in f.columns:
            cols["oi"] = "fut_oi"
        bars = bars.join(_by_available(f, cols), how="left")
    return bars


def chain_from_rolling(frames: Iterable[pd.DataFrame]) -> pd.DataFrame:
    """Dhan expired-options candles (one frame per relative strike/side) -> chain table."""
    parts = []
    for fr in frames:
        if fr.empty:
            continue
        missing = {"available_at", "strike", "option_type", "close"} - set(fr.columns)
        if missing:
            log.warning("skipping option frame without columns %s", sorted(missing))
            continue
        parts.append(
            pd.DataFrame(
                {
                    "available_at": fr["available_at"],
                    "strike": fr["strike"].astype(float),
                    "option_type": fr["option_type"],
                    "ltp": fr["close"].astype(float),
                    "oi": fr.get("oi", pd.Series(np.nan, index=fr.index)).astype(float),
                    "volume": fr.get("volume", pd.Series(np.nan, index=fr.index)).astype(float),
                    "iv": fr.get("iv", pd.Series(np.nan, index=fr.index)).astype(float),
                    "underlying": fr.get("spot", pd.Series(np.nan, index=fr.index)).astype(float),
                    "prev_oi": np.nan,
                }
            )
        )
    if not parts:
        return pd.DataFrame(columns=CHAIN_COLUMNS)
    return pd.concat(parts, ignore_index=True).sort_values("available_at")


def daily_context(
    days: Iterable[date],
    calendar: TradingCalendar,
    expiry: ExpiryRules | None = None,
    flows: FlowStore | None = None,
) -> pd.DataFrame:
    """Per-day facts known before the open. Unknown values stay NaN."""
    rows = []
    for d in sorted(set(days)):
        row: dict[str, Any] = {"date": d}
        try:
            events = calendar.events_on(d)
            row["event_high"] = float(any(e.impact == "high" for e in events))
            row["event_medium"] = float(any(e.impact == "medium" for e in events))
            if expiry is not None:
                dte = expiry.days_to_expiry(d)
                row["days_to_expiry"] = float(dte)
                row["is_expiry_day"] = float(dte == 0)
        except CalendarError:
            pass
        if flows is not None:
            hist = flows.participant_history("FII", d, limit=2)  # strictly before d
            if hist:
                row["fii_fut_long_ratio"] = hist[-1].fut_long_ratio
            if len(hist) == 2:
                row["fii_fut_net_chg"] = float(hist[-1].fut_net - hist[-2].fut_net)
            cash = flows.cash_until(d, limit=1)
            if cash:
                row["fii_cash_cr"] = cash[-1].fii_net_cr
                row["dii_cash_cr"] = cash[-1].dii_net_cr
        rows.append(row)
    return pd.DataFrame(rows).set_index("date") if rows else pd.DataFrame()
