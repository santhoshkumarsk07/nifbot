"""Intraday price / futures / VIX features for ONE session day of 1-minute bars.

Only causal operations are used (shift, cumulative, trailing rolling windows),
so the value at a bar never depends on later bars.
"""

from __future__ import annotations

import math
from datetime import time

import numpy as np
import pandas as pd

OPEN = time(9, 15)
CLOSE = time(15, 30)
BARS_PER_DAY = 375

PRICE_FEATURES = (
    "minutes_since_open",
    "minutes_to_close",
    "ret_1",
    "ret_5",
    "ret_15",
    "ret_30",
    "rv_15",
    "rv_30",
    "dist_open_pct",
    "range_pos",
    "gap_pct",
    "or15_pos",
    "or30_pos",
    "fut_basis_pct",
    "fut_vwap_dist_pct",
    "fut_ret_15",
    "fut_oi_chg_15",
    "buildup_15",
    "vix",
    "vix_chg_15",
)


def _minutes(idx: pd.DatetimeIndex, t: time) -> np.ndarray:
    mins = idx.hour * 60 + idx.minute + idx.second / 60
    return np.asarray(mins - (t.hour * 60 + t.minute), dtype=float)


def _opening_range(s: pd.Series, minutes: int) -> tuple[pd.Series, pd.Series]:
    """High/low of the first ``minutes``; NaN until that window has completed."""
    since = _minutes(pd.DatetimeIndex(s.index), OPEN)
    inside = since <= minutes
    hi = s.where(inside).cummax().ffill()
    lo = s.where(inside).cummin().ffill()
    done = since >= minutes
    return hi.where(done), lo.where(done)


def _col(day: pd.DataFrame, name: str) -> pd.Series:
    if name in day.columns:
        return day[name].astype(float)
    return pd.Series(np.nan, index=day.index, dtype=float)


def buildup(price_chg: pd.Series, oi_chg: pd.Series) -> pd.Series:
    """+2 long build-up, -2 short build-up, +1 short covering, -1 long unwinding, 0 flat."""
    out = pd.Series(0.0, index=price_chg.index)
    out[(price_chg > 0) & (oi_chg > 0)] = 2
    out[(price_chg < 0) & (oi_chg > 0)] = -2
    out[(price_chg > 0) & (oi_chg < 0)] = 1
    out[(price_chg < 0) & (oi_chg < 0)] = -1
    return out.where(price_chg.notna() & oi_chg.notna())


def day_features(day: pd.DataFrame, prev_close: float) -> pd.DataFrame:
    """Features for one session day. ``day`` index = available_at (IST), sorted."""
    idx = pd.DatetimeIndex(day.index)
    s = day["spot"].astype(float)
    f = pd.DataFrame(index=day.index)
    f["minutes_since_open"] = _minutes(idx, OPEN)
    f["minutes_to_close"] = -_minutes(idx, CLOSE)
    logp = pd.Series(np.log(s.to_numpy()), index=s.index)
    r1 = logp.diff()
    f["ret_1"] = r1 * 100
    for n in (5, 15, 30):
        f[f"ret_{n}"] = (logp - logp.shift(n)) * 100
    ann = math.sqrt(BARS_PER_DAY) * 100  # per-day volatility in %
    f["rv_15"] = r1.rolling(15, min_periods=10).std() * ann
    f["rv_30"] = r1.rolling(30, min_periods=20).std() * ann
    f["dist_open_pct"] = (s / s.iloc[0] - 1) * 100
    hi, lo = s.cummax(), s.cummin()
    f["range_pos"] = ((s - lo) / (hi - lo)).where(hi > lo)
    f["gap_pct"] = (s.iloc[0] / prev_close - 1) * 100 if prev_close > 0 else np.nan
    for m in (15, 30):
        ohi, olo = _opening_range(s, m)
        f[f"or{m}_pos"] = ((s - olo) / (ohi - olo)).where(ohi > olo)

    fut = _col(day, "fut")
    f["fut_basis_pct"] = (fut / s - 1) * 100
    f["fut_vwap_dist_pct"] = (fut / _col(day, "fut_vwap") - 1) * 100
    fut_chg = fut / fut.shift(15) - 1
    oi = _col(day, "fut_oi")
    oi_chg = oi / oi.shift(15) - 1
    f["fut_ret_15"] = fut_chg * 100
    f["fut_oi_chg_15"] = oi_chg * 100
    f["buildup_15"] = buildup(fut_chg, oi_chg)

    vix = _col(day, "vix")
    f["vix"] = vix
    f["vix_chg_15"] = vix - vix.shift(15)
    return f
