"""SYNTHETIC market sessions for feature tests. Random walks, NOT real data."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

from nifbot.news.models import NewsItem, ScoredNews
from nifbot.timeutil import IST

DAYS = (date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30))


def bar_times(day: date) -> pd.DatetimeIndex:
    """available_at of 1-minute candles 09:15..15:29 -> 09:16..15:30."""
    start = datetime.combine(day, time(9, 16), tzinfo=IST)
    return pd.DatetimeIndex([start + timedelta(minutes=i) for i in range(375)])


def make_bars(days: tuple[date, ...] = DAYS, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    frames = []
    level = 25000.0
    oi = 12_000_000.0
    for d in days:
        idx = bar_times(d)
        level *= float(np.exp(rng.normal(0, 0.004)))  # overnight gap
        spot = level * np.exp(np.cumsum(rng.normal(0, 0.0006, len(idx))))
        level = float(spot[-1])
        fut = spot * 1.002 + rng.normal(0, 2, len(idx))
        oi_path = oi * np.exp(np.cumsum(rng.normal(0, 0.001, len(idx))))
        vwap = pd.Series(fut).expanding().mean().to_numpy()
        vix = 13 + np.cumsum(rng.normal(0, 0.02, len(idx)))
        frames.append(
            pd.DataFrame(
                {"spot": spot, "fut": fut, "fut_oi": oi_path, "fut_vwap": vwap, "vix": vix},
                index=idx,
            )
        )
    return pd.concat(frames)


def make_chain(bars: pd.DataFrame, every: int = 3, seed: int = 11) -> pd.DataFrame:
    """Chain snapshots every `every` minutes around the spot (ATM +/- 10 strikes)."""
    rng = np.random.default_rng(seed)
    rows = []
    spots = bars["spot"].to_numpy(dtype=float)
    for i in range(0, len(bars), every):
        t, spot = bars.index[i], float(spots[i])
        atm = round(spot / 50) * 50
        for k in range(atm - 500, atm + 550, 50):
            dist = (k - spot) / spot
            for ot in ("CE", "PE"):
                intrinsic = max(0.0, spot - k) if ot == "CE" else max(0.0, k - spot)
                wall = (
                    3.0
                    if (ot == "CE" and k == atm + 300) or (ot == "PE" and k == atm - 200)
                    else 1.0
                )
                rows.append(
                    {
                        "available_at": t,
                        "strike": float(k),
                        "option_type": ot,
                        "ltp": intrinsic + max(1.0, 120 * np.exp(-abs(dist) * 60)),
                        "oi": float(
                            int(wall * 1e6 * np.exp(-abs(dist) * 30) + rng.integers(0, 1000))
                        ),
                        "volume": float(rng.integers(100, 10000)),
                        "iv": 12 + 40 * dist**2 * 100 + (1.5 if ot == "PE" and k < spot else 0.0),
                        "underlying": spot,
                        "prev_oi": np.nan,
                    }
                )
    return pd.DataFrame(rows)


def make_news(days: tuple[date, ...] = DAYS) -> list[ScoredNews]:
    out = []
    for i, d in enumerate(days):
        for h, sent in ((8, 0.5), (11, -0.6), (14, 0.3)):
            at = datetime.combine(d, time(h, 7), tzinfo=IST)
            item = NewsItem(
                source_id="syn",
                source_name="Synthetic",
                title=f"synthetic {i} {h}",
                url=f"https://synthetic.test/{d}/{h}",
                fetched_at=at,
            )
            out.append(ScoredNews(item=item, tags=("index",), sentiment=sent, impact="high"))
    return out
