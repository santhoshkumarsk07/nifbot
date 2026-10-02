"""LOOK-AHEAD TESTS. These must fail if any feature uses data from after its timestamp."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from nifbot.features import FEATURE_COLUMNS, FeatureInputs, build_features
from nifbot.features.inputs import daily_context
from nifbot.flows import participant_oi
from nifbot.flows.store import FlowStore
from nifbot.news.store import connect
from nifbot.trading_calendar import TradingCalendar
from tests.fakes_web import PARTICIPANT_CSV
from tests.synth import make_bars, make_chain, make_news


@pytest.fixture(scope="module")
def full() -> tuple[FeatureInputs, pd.DataFrame]:
    bars = make_bars()
    inp = FeatureInputs(bars=bars, chain=make_chain(bars), news=make_news())
    return inp, build_features(inp)


def _truncate(inp: FeatureInputs, t: pd.Timestamp) -> FeatureInputs:
    chain = inp.chain
    assert chain is not None
    return FeatureInputs(
        bars=inp.bars[inp.bars.index <= t],
        chain=chain[chain["available_at"] <= t],
        news=[n for n in inp.news if n.item.fetched_at <= t],
    )


def _same(a: pd.Series, b: pd.Series) -> None:
    for col in FEATURE_COLUMNS:
        x, y = a[col], b[col]
        assert (pd.isna(x) and pd.isna(y)) or x == pytest.approx(y, rel=1e-9, abs=1e-12), col


def test_truncation_invariance(full: tuple[FeatureInputs, pd.DataFrame]) -> None:
    """Features at t from ALL data == features at t from data available at t."""
    inp, feats = full
    rng = np.random.default_rng(1)
    picks = list(rng.choice(feats.index, size=25, replace=False))
    picks += [feats.index[0], feats.index[-1], feats.index[375], feats.index[400]]
    for t in picks:
        t = pd.Timestamp(t)
        _same(feats.loc[t], build_features(_truncate(inp, t)).loc[t])


def test_future_perturbation(full: tuple[FeatureInputs, pd.DataFrame]) -> None:
    """Scrambling everything after t must not change any feature at or before t."""
    inp, feats = full
    assert inp.chain is not None
    t = feats.index[500]
    bars = inp.bars.copy()
    after = bars.index > t
    bars.loc[after] = bars.loc[after] * np.random.default_rng(2).uniform(
        0.5, 1.5, bars.loc[after].shape
    )
    chain = inp.chain.copy()
    late = chain["available_at"] > t
    chain.loc[late, "oi"] = chain.loc[late, "oi"] * 7
    chain.loc[late, "iv"] = 99.0
    news = [
        *inp.news,
        make_news()[0].model_copy(
            update={
                "item": make_news()[0].item.model_copy(
                    update={"fetched_at": t + timedelta(minutes=1)}
                ),
                "sentiment": -1.0,
            }
        ),
    ]
    changed = build_features(FeatureInputs(bars=bars, chain=chain, news=news))
    before = feats.index <= t
    pd.testing.assert_frame_equal(feats[before], changed[before])
    assert not feats[~before].equals(changed[~before])  # sanity: the future did change


def test_chain_snapshot_not_used_before_it_arrives(
    full: tuple[FeatureInputs, pd.DataFrame],
) -> None:
    inp, feats = full
    assert inp.chain is not None
    first_chain = inp.chain["available_at"].min()
    assert feats.loc[feats.index < first_chain, "pcr_oi"].isna().all()
    assert feats.loc[first_chain, "pcr_oi"] > 0


def test_daily_context_uses_only_previous_days(tmp_path: Path) -> None:
    store = FlowStore(connect(tmp_path / "f.sqlite3"))
    day = date(2026, 9, 29)
    store.add_participant(participant_oi.parse(PARTICIPANT_CSV, day))
    ctx = daily_context([day, date(2026, 9, 30)], TradingCalendar.load(), flows=store)
    assert pd.isna(ctx.loc[day, "fii_fut_long_ratio"]) if "fii_fut_long_ratio" in ctx else True
    assert ctx.loc[date(2026, 9, 30), "fii_fut_long_ratio"] == 0.25


def test_opening_range_unknown_until_complete(full: tuple[FeatureInputs, pd.DataFrame]) -> None:
    _, feats = full
    day = feats[feats.index.date == feats.index[0].date()]
    mins = day["minutes_since_open"]
    assert day.loc[mins < 15, "or15_pos"].isna().all()
    assert day.loc[mins >= 15, "or15_pos"].notna().all()
    assert day.loc[mins < 30, "or30_pos"].isna().all()
