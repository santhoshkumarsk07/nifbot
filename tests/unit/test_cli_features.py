"""CLI: features (recorded day) and features-history (Dhan history files). Synthetic data."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from nifbot import cli
from nifbot.data.models import OptionChain, OptionQuote, Quote, Snapshot
from tests.synth import make_bars


def test_features_for_recorded_day(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = cli.PROJECT_ROOT
    bars = make_bars(days=(date(2026, 9, 29),)).iloc[:40]
    day_dir = root / "data" / "recorded" / "2026-09-29"
    day_dir.mkdir(parents=True)
    lines = []
    for t, row in bars.iterrows():
        ts = pd.Timestamp(t).to_pydatetime()
        q = Quote(symbol="NIFTY", security_id="13", ltp=float(row["spot"]), received_at=ts)
        rows = tuple(
            OptionQuote(strike=float(k), option_type=ot, ltp=50.0, oi=1000 + k % 7, volume=5)
            for k in range(24800, 25300, 50)
            for ot in ("CE", "PE")
        )
        chain = OptionChain(
            underlying="NIFTY",
            expiry=date(2026, 9, 29),
            received_at=ts,
            underlying_ltp=float(row["spot"]),
            rows=rows,
        )
        lines.append(Snapshot(received_at=ts, spot=q, vix=q, chain=chain).model_dump_json())
    (day_dir / "snapshots.jsonl").write_text("\n".join(lines) + "\n")
    assert cli.main(["features", "2026-09-29"]) == 0
    out = capsys.readouterr().out
    assert "rows: 40" in out and "pcr_oi" in out and (day_dir / "features.parquet").exists()
    assert cli.main(["features", "2026-09-30"]) == 1


def test_features_history(capsys: pytest.CaptureFixture[str]) -> None:
    root = cli.PROJECT_ROOT
    assert cli.main(["features-history"]) == 1
    assert "make fetch-history" in capsys.readouterr().out
    hist = root / "data" / "history" / "dhan"
    hist.mkdir(parents=True)
    bars = make_bars(days=(date(2026, 9, 28), date(2026, 9, 29)))
    frame = pd.DataFrame(
        {
            "available_at": bars.index,
            "start": pd.DatetimeIndex(bars.index) - pd.Timedelta(minutes=1),
            "close": bars["spot"].to_numpy(),
        }
    )
    frame.to_parquet(hist / "spot_2026-09-28_2026-09-29.parquet")
    vix = frame.assign(close=bars["vix"].to_numpy())
    vix.to_parquet(hist / "vix_2026-09-28_2026-09-29.parquet")
    opt = frame.assign(
        strike=25000.0,
        option_type="CE",
        oi=1000.0,
        iv=12.0,
        volume=10.0,
        spot=bars["spot"].to_numpy(),
        close=55.0,
    )
    opt.to_parquet(hist / "options_week1_ATM_CALL_x.parquet")
    assert cli.main(["features-history"]) == 0
    out = capsys.readouterr().out
    assert "rows: 750" in out and "option files: 1" in out
    feats = pd.read_parquet(root / "data" / "features" / "history.parquet")
    assert feats["days_to_expiry"].notna().all() and feats["vix"].notna().all()
