"""Feature calculations on small hand-checked examples (synthetic)."""

from __future__ import annotations

import math
from datetime import date, datetime, time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from nifbot.data.models import OptionChain, OptionQuote, Quote, Snapshot
from nifbot.features import FEATURE_COLUMNS, FeatureInputs, build_features
from nifbot.features.expiry import ExpiryRules
from nifbot.features.inputs import (
    bars_from_history,
    bars_from_snapshots,
    chain_from_rolling,
    chain_from_snapshots,
    daily_context,
)
from nifbot.features.options import chain_metrics, max_pain, strike_step
from nifbot.features.price import buildup
from nifbot.flows import fii_dii
from nifbot.flows.store import FlowStore
from nifbot.news.store import connect
from nifbot.timeutil import IST
from nifbot.trading_calendar import CalendarError, MarketEvent, TradingCalendar
from tests.synth import bar_times, make_bars, make_chain


def _snap(rows: list[tuple[float, str, float, float]]) -> pd.DataFrame:
    """rows: (strike, type, oi, ltp)"""
    return pd.DataFrame(
        [
            {
                "strike": k,
                "option_type": t,
                "oi": oi,
                "ltp": ltp,
                "volume": oi / 10,
                "iv": 15.0 if t == "CE" else 17.0,
                "oi_chg": oi / 5,
            }
            for k, t, oi, ltp in rows
        ]
    )


def test_max_pain_hand_example() -> None:
    strikes = np.array([100.0, 110.0, 120.0])
    # payout at 100: 700, at 110: 200, at 120: 700 -> max pain 110
    assert max_pain(strikes, np.array([10.0, 50, 0]), np.array([0.0, 50, 10])) == 110
    assert math.isnan(max_pain(strikes, np.zeros(3), np.zeros(3)))
    assert strike_step(np.array([100.0, 150, 200, 300])) == 50
    assert math.isnan(strike_step(np.array([100.0])))


def test_chain_metrics() -> None:
    snap = _snap(
        [
            (24900, "CE", 100, 160),
            (24900, "PE", 400, 40),
            (25000, "CE", 300, 90),
            (25000, "PE", 300, 80),
            (25100, "CE", 900, 40),
            (25100, "PE", 100, 150),
        ]
    )
    m = chain_metrics(snap, spot=25010, atm_width=1, skew_steps=1)
    assert m["pcr_oi"] == pytest.approx(800 / 1300)
    assert m["call_wall"] == 25100 and m["put_wall"] == 24900
    assert m["call_wall_dist_pct"] == pytest.approx((25100 / 25010 - 1) * 100)
    assert m["atm_iv"] == pytest.approx(16.0)
    assert m["iv_skew"] == pytest.approx(17 - 15)
    assert m["atm_straddle_pct"] == pytest.approx(170 / 25010 * 100)
    assert m["oi_chg_ratio_atm"] == pytest.approx((160 - 260) / 2100)
    assert all(math.isnan(v) for v in chain_metrics(snap.iloc[0:0], 25000).values())
    assert math.isnan(chain_metrics(snap, float("nan"))["pcr_oi"])


def test_buildup_codes() -> None:
    p = pd.Series([1.0, -1.0, 1.0, -1.0, 0.0, np.nan])
    oi = pd.Series([1.0, 1.0, -1.0, -1.0, 1.0, 1.0])
    assert buildup(p, oi).tolist()[:5] == [2, -2, 1, -1, 0]
    assert math.isnan(buildup(p, oi).iloc[5])


def test_build_features_shape_and_values() -> None:
    bars = make_bars()
    feats = build_features(FeatureInputs(bars=bars, chain=make_chain(bars)))
    assert list(feats.columns) == list(FEATURE_COLUMNS)
    assert len(feats) == 3 * 375
    day2 = feats[feats.index.date == date(2026, 9, 29)]
    assert day2["minutes_since_open"].iloc[0] == 1 and day2["minutes_to_close"].iloc[-1] == 0
    prev_close = bars[bars.index.date == date(2026, 9, 28)]["spot"].iloc[-1]
    open2 = bars[bars.index.date == date(2026, 9, 29)]["spot"].iloc[0]
    assert day2["gap_pct"].iloc[0] == pytest.approx((open2 / prev_close - 1) * 100)
    assert feats["gap_pct"].iloc[:375].isna().all()  # no previous day for day 1
    assert feats["call_wall_dist_pct"].dropna().gt(0).all()
    assert feats["put_wall_dist_pct"].dropna().lt(0).all()
    assert feats["iv_skew"].dropna().gt(0).mean() > 0.9  # synthetic puts are richer
    assert feats["news_score"].eq(0).all()  # no news given -> neutral, not invented
    assert feats["days_to_expiry"].isna().all()  # no daily context -> NaN


def test_session_filter_and_errors() -> None:
    bars = make_bars(days=(date(2026, 9, 28),))
    extra = pd.DataFrame(
        {"spot": [1.0, 25000.0]},
        index=pd.DatetimeIndex(
            [datetime(2026, 9, 28, 9, 10, tzinfo=IST), datetime(2026, 9, 28, 15, 40, tzinfo=IST)]
        ),
    )
    feats = build_features(FeatureInputs(bars=pd.concat([bars, extra]).sort_index()))
    assert len(feats) == 375
    naive = bars.copy()
    naive.index = naive.index.tz_localize(None)
    with pytest.raises(ValueError):
        build_features(FeatureInputs(bars=naive))
    assert build_features(FeatureInputs(bars=bars.iloc[0:0])).empty


def test_expiry_rules() -> None:
    cal = TradingCalendar.load()
    rules = ExpiryRules.load(cal)
    assert rules.expiry_on_or_after(date(2025, 8, 18)) == date(2025, 8, 21)  # Thu
    assert rules.expiry_on_or_after(date(2024, 8, 12)) == date(2024, 8, 14)  # Thu holiday -> Wed
    assert rules.expiry_on_or_after(date(2026, 9, 28)) == date(2026, 9, 29)  # Tue
    assert rules.expiry_on_or_after(date(2026, 3, 2)) == date(2026, 3, 2)  # Holi Tue -> Mon
    assert rules.expiry_on_or_after(date(2026, 3, 4)) == date(2026, 3, 10)
    assert rules.days_to_expiry(date(2026, 9, 28)) == 1
    assert rules.days_to_expiry(date(2026, 9, 29)) == 0
    with pytest.raises(CalendarError):
        rules.expiry_on_or_after(date(2019, 1, 1))
    with pytest.raises(CalendarError):
        rules.expiry_on_or_after(date(2030, 6, 3))  # holidays unknown -> refuse


def test_daily_context(tmp_path: Path) -> None:
    store = FlowStore(connect(tmp_path / "f.sqlite3"))
    store.add_cash(fii_dii.CashFlow(date(2026, 9, 28), -1200.0, 900.0), "manual")
    cal = TradingCalendar(
        years=TradingCalendar.load().years,
        events=(MarketEvent(date=date(2026, 9, 29), name="RBI", impact="high"),),
    )
    ctx = daily_context([date(2026, 9, 29), date(2030, 1, 7)], cal, ExpiryRules.load(cal), store)
    row = ctx.loc[date(2026, 9, 29)]
    assert row["event_high"] == 1 and row["is_expiry_day"] == 1 and row["fii_cash_cr"] == -1200
    assert pd.isna(ctx.loc[date(2030, 1, 7)].get("days_to_expiry"))
    assert daily_context([], cal).empty


def test_inputs_from_snapshots() -> None:
    t = datetime(2026, 9, 29, 10, 0, tzinfo=IST)
    q = Quote(symbol="X", security_id="1", ltp=25000, received_at=t)
    fut = Quote(symbol="F", security_id="2", ltp=25050, oi=100, avg_price=25040, received_at=t)
    chain = OptionChain(
        underlying="NIFTY",
        expiry=date(2026, 9, 29),
        underlying_ltp=25000,
        received_at=t,
        rows=(OptionQuote(strike=25000, option_type="CE", ltp=50, oi=10, prev_oi=4, volume=1),),
    )
    snaps = [
        Snapshot(received_at=t, spot=q, futures=fut, vix=q, chain=chain),
        Snapshot(received_at=t),
    ]
    bars = bars_from_snapshots(snaps)
    assert bars.loc[t, "fut_vwap"] == 25040 and len(bars) == 1
    ch = chain_from_snapshots(snaps)
    assert ch.iloc[0]["prev_oi"] == 4 and ch.iloc[0]["underlying"] == 25000
    assert bars_from_snapshots([Snapshot(received_at=t)]).empty


def test_inputs_from_history() -> None:
    idx = bar_times(date(2026, 9, 29))[:3]
    start = idx - pd.Timedelta(minutes=1)
    spot = pd.DataFrame({"start": start, "available_at": idx, "close": [1.0, 2, 3]})
    vix = pd.DataFrame({"start": start, "available_at": idx, "close": [13.0, 13.1, 13.2]})
    fut = pd.DataFrame(
        {
            "start": start,
            "available_at": idx,
            "high": [2.0, 3, 4],
            "low": [0.0, 1, 2],
            "close": [1.0, 2, 3],
            "volume": [10, 10, 20],
            "oi": [5, 6, 7],
        }
    )
    bars = bars_from_history(spot, vix, fut)
    assert list(bars.columns) == ["spot", "vix", "fut", "fut_vwap", "fut_oi"]
    assert bars["fut_vwap"].iloc[-1] == pytest.approx((10 * 1 + 10 * 2 + 20 * 3) / 40)
    roll = pd.DataFrame(
        {
            "available_at": idx,
            "strike": [25000, 25000, 25050],
            "close": [1.0, 2, 3],
            "oi": [1, 2, 3],
            "iv": [12.0, 12, 12],
            "spot": [25010.0, 25012, 25030],
            "option_type": "CE",
            "volume": [1, 1, 1],
        }
    )
    ch = chain_from_rolling([roll, pd.DataFrame()])
    assert len(ch) == 3 and ch["underlying"].iloc[0] == 25010
    assert chain_from_rolling([]).empty
    assert chain_from_rolling([roll.drop(columns=["strike"])]).empty  # malformed -> skipped


def test_chain_oi_change_from_day_start_when_no_prev_oi() -> None:
    from nifbot.features.build import chain_block

    t0 = datetime.combine(date(2026, 9, 29), time(9, 20), tzinfo=IST)
    t1 = datetime.combine(date(2026, 9, 29), time(9, 21), tzinfo=IST)
    rows = []
    for t, oi in ((t0, 100.0), (t1, 160.0)):
        for ot in ("CE", "PE"):
            rows.append(
                {
                    "available_at": t,
                    "strike": 25000.0,
                    "option_type": ot,
                    "ltp": 50.0,
                    "oi": oi,
                    "volume": 1.0,
                    "iv": 12.0,
                    "underlying": 25000.0,
                    "prev_oi": np.nan,
                }
            )
    cb = chain_block(pd.DataFrame(rows))
    assert cb.loc[t0, "ce_oi_chg_atm"] == 0 and cb.loc[t1, "ce_oi_chg_atm"] == 60
