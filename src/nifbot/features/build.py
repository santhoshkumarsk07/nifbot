"""build_features: the single entry point used by backtest, training and live."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from itertools import pairwise

import numpy as np
import pandas as pd

from nifbot.features.options import CHAIN_FEATURES, chain_metrics_arrays
from nifbot.features.price import CLOSE, OPEN, PRICE_FEATURES, day_features
from nifbot.news.models import ScoredNews
from nifbot.news.score import news_score

CONTEXT_FEATURES = (
    "news_score",
    "event_high",
    "event_medium",
    "days_to_expiry",
    "is_expiry_day",
    "fii_fut_long_ratio",
    "fii_fut_net_chg",
    "fii_cash_cr",
    "dii_cash_cr",
)
FEATURE_COLUMNS: tuple[str, ...] = PRICE_FEATURES + CHAIN_FEATURES + CONTEXT_FEATURES


@dataclass(frozen=True)
class FeatureInputs:
    bars: pd.DataFrame
    chain: pd.DataFrame | None = None
    daily: pd.DataFrame | None = None
    news: Sequence[ScoredNews] = field(default_factory=tuple)
    half_life_minutes: float = 120.0
    chain_tolerance: timedelta = timedelta(minutes=3)


def _session(bars: pd.DataFrame) -> pd.DataFrame:
    idx = pd.DatetimeIndex(bars.index)
    if idx.tz is None:
        raise ValueError("bars index must be timezone-aware (IST)")
    t = idx.time
    keep = (
        (t > OPEN) & (t <= CLOSE) & bars["spot"].notna().to_numpy() & (bars["spot"] > 0).to_numpy()
    )
    return bars[keep].sort_index()


def _price_block(bars: pd.DataFrame) -> pd.DataFrame:
    parts = []
    prev_close = float("nan")
    for _, day in bars.groupby(pd.DatetimeIndex(bars.index).date, sort=True):
        parts.append(day_features(day, prev_close))
        prev_close = float(day["spot"].iloc[-1])
    return pd.concat(parts) if parts else pd.DataFrame(columns=list(PRICE_FEATURES))


def chain_block(chain: pd.DataFrame) -> pd.DataFrame:
    """Chain metrics per snapshot time (index = available_at)."""
    if chain.empty:
        return pd.DataFrame(columns=list(CHAIN_FEATURES))
    c = chain.sort_values("available_at").copy()
    c["day"] = pd.to_datetime(c["available_at"]).dt.date
    first = c.groupby(["day", "strike", "option_type"])["oi"].transform("first")
    prev = c["prev_oi"] if "prev_oi" in c.columns else pd.Series(np.nan, index=c.index)
    c["oi_chg"] = np.where(prev.notna(), c["oi"] - prev, c["oi"] - first)
    wide = c.pivot_table(
        index=["available_at", "strike"],
        columns="option_type",
        values=["oi", "volume", "iv", "ltp", "oi_chg"],
        aggfunc="last",
    ).sort_index()
    und = c.groupby("available_at")["underlying"].last()
    times = wide.index.get_level_values(0)
    time_list = list(times)
    strikes_all = wide.index.get_level_values(1).to_numpy(dtype=float)
    cols: dict[str, np.ndarray] = {}
    for f in ("oi", "volume", "iv", "ltp", "oi_chg"):
        for side in ("CE", "PE"):
            key = f"{side.lower()}_{f}"
            cols[key] = (
                wide[(f, side)].to_numpy(dtype=float)
                if (f, side) in wide.columns
                else np.full(len(wide), np.nan)
            )
    bounds = np.flatnonzero(np.r_[True, times[1:] != times[:-1], True])
    rows = {}
    for b0, b1 in pairwise(bounds):
        t = time_list[b0]
        arrays = {k: v[b0:b1] for k, v in cols.items()}
        rows[t] = chain_metrics_arrays(strikes_all[b0:b1], arrays, float(und.loc[t]))
    out = pd.DataFrame.from_dict(rows, orient="index")
    out.index = pd.DatetimeIndex(out.index)
    return out.sort_index()


def _news_block(
    index: pd.DatetimeIndex, news: Sequence[ScoredNews], half_life: float
) -> np.ndarray:
    items = sorted(news, key=lambda s: s.item.fetched_at)
    times = [s.item.fetched_at for s in items]
    window = timedelta(hours=24)
    out = np.zeros(len(index))
    for i, t in enumerate(index):
        lo = bisect_left(times, t - window)
        hi = bisect_right(times, t)
        out[i] = news_score(items[lo:hi], t.to_pydatetime(), half_life)
    return out


def build_features(inp: FeatureInputs) -> pd.DataFrame:
    """Feature matrix indexed by decision time (each bar's available_at).

    Columns are exactly FEATURE_COLUMNS. Missing inputs give NaN, never guesses.
    """
    bars = _session(inp.bars)
    feats = _price_block(bars)
    if feats.empty:
        return pd.DataFrame(columns=list(FEATURE_COLUMNS))
    feats.index = pd.DatetimeIndex(feats.index)

    if inp.chain is not None and not inp.chain.empty:
        cb = chain_block(inp.chain)
        cb.index = pd.DatetimeIndex(cb.index).tz_convert(feats.index.tz)
        merged = pd.merge_asof(
            feats[[]].reset_index(names="t"),
            cb.reset_index(names="ct"),
            left_on="t",
            right_on="ct",
            direction="backward",
            tolerance=inp.chain_tolerance,
        ).set_index("t")
        for col in CHAIN_FEATURES:
            feats[col] = merged[col].to_numpy()
    else:
        for col in CHAIN_FEATURES:
            feats[col] = np.nan

    feats["news_score"] = _news_block(feats.index, inp.news, inp.half_life_minutes)
    days = pd.Index(feats.index.date)
    for col in CONTEXT_FEATURES[1:]:
        if inp.daily is not None and col in inp.daily.columns:
            feats[col] = inp.daily[col].reindex(days).to_numpy(dtype=float)
        else:
            feats[col] = np.nan
    return feats[list(FEATURE_COLUMNS)]
