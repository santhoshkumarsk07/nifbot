"""data-check, flows-backfill and selftest (all offline with fakes)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import pytest

from nifbot import cli, readiness, selftest, services
from nifbot.config import Secrets, load_settings
from nifbot.data.dhan import DhanAdapter, DhanClient
from nifbot.flows import participant_oi
from nifbot.flows.store import FlowStore
from nifbot.net import FetchError
from nifbot.news.sources import load_news_config
from nifbot.news.store import connect
from nifbot.trading_calendar import TradingCalendar
from tests.dhan_fakes import FUT_ID, FakeDhan
from tests.fakes_web import PARTICIPANT_CSV, FakeFetcher, fred_csv, rss
from tests.synth import make_bars

TODAY = date(2026, 10, 5)


def _write_history(hist: Path, days: tuple[date, ...]) -> None:
    hist.mkdir(parents=True, exist_ok=True)
    bars = make_bars(days=days)
    frame = pd.DataFrame({"available_at": bars.index, "close": bars["spot"].to_numpy()})
    frame.to_parquet(hist / "spot_a.parquet")
    frame.to_parquet(hist / "vix_a.parquet")
    for rel in ("ATM", "ATM+1"):
        opt = frame.assign(strike=25000.0, option_type="CE", oi=10.0)
        opt.to_parquet(hist / f"options_week1_{rel}_CALL_x.parquet")


def test_check_history_missing_and_partial(tmp_path: Path) -> None:
    cal = TradingCalendar.load()
    checks, days = readiness.check_history(tmp_path / "none", cal, TODAY)
    assert checks[0].status == "MISSING" and days == set()
    _write_history(tmp_path / "h", (date(2026, 9, 28), date(2026, 9, 30)))
    checks, days = readiness.check_history(tmp_path / "h", cal, TODAY)
    by = {c.name: c for c in checks}
    assert by["Nifty 1-min history"].status == "WARN"  # far less than 2 years, 1 day gap
    assert "1 trading days missing" in by["Nifty 1-min history"].detail
    assert by["India VIX 1-min history"].status == "OK"
    assert by["Expired options history"].status == "WARN"  # only 2 relative strikes
    assert by["Futures 1-min history"].status == "INFO"
    assert len(days) == 2


def test_check_features_flows_config_env(tmp_path: Path) -> None:
    assert readiness.check_features(tmp_path / "x.parquet").status == "MISSING"
    feats = pd.DataFrame(
        {"a": [1.0, 2.0], "b": [None, None]},
        index=pd.DatetimeIndex(["2026-09-28 10:00", "2026-09-29 10:00"]),
    )
    feats.to_parquet(tmp_path / "f.parquet")
    c = readiness.check_features(tmp_path / "f.parquet")
    assert c.status == "OK" and "mostly empty: b" in c.detail

    conn = connect(tmp_path / "db.sqlite3")
    flows = readiness.check_flows(conn, {date(2026, 9, 28)})
    assert flows[0].status == "MISSING" and flows[2].name == "News archive"
    FlowStore(conn).add_participant(participant_oi.parse(PARTICIPANT_CSV, date(2026, 9, 28)))
    assert readiness.check_flows(conn, {date(2026, 9, 28)})[0].status == "OK"

    cfg = readiness.check_config(TradingCalendar.load(), date(2021, 1, 4), TODAY)
    assert cfg[0].status == "MISSING" and "2021" in cfg[0].detail
    assert readiness.check_config(TradingCalendar.load(), None, TODAY)[1].status == "WARN"

    env = readiness.check_environment(Secrets(_env_file=None), tmp_path, tmp_path / "rec")
    assert env[0].status == "MISSING" and env[1].status == "WARN"
    assert not readiness.training_ready([readiness.Check("Nifty 1-min history", "MISSING", "")])


def test_backfill_participant_oi(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite3")
    url = participant_oi.URL.format(d=date(2026, 9, 29))
    fetcher = FakeFetcher({url: PARTICIPANT_CSV.encode()})
    got = services.backfill_participant_oi(
        conn, fetcher, TradingCalendar.load(), date(2026, 9, 26), date(2026, 9, 30)
    )
    fetched, skipped, failures = got
    assert fetched == 1 and skipped == 0 and len(failures) == 2  # 28th and 30th not faked
    again = services.backfill_participant_oi(
        conn, fetcher, TradingCalendar.load(), date(2026, 9, 29), date(2026, 9, 29)
    )
    assert again == (0, 1, [])
    unknown_year = services.backfill_participant_oi(
        conn, fetcher, TradingCalendar.load(), date(2031, 1, 6), date(2031, 1, 7)
    )
    assert unknown_year == (0, 0, [])


def test_data_check_cli(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["data-check"]) == 1
    out = capsys.readouterr().out
    assert "[MISSING] Nifty 1-min history" in out and "NOT READY" in out
    hist = cli.PROJECT_ROOT / "data" / "history" / "dhan"
    _write_history(hist, (date(2026, 9, 28), date(2026, 9, 29)))
    assert cli.main(["features-history"]) == 0
    capsys.readouterr()
    assert cli.main(["data-check"]) == 0
    assert "READY for backtesting/training" in capsys.readouterr().out


def test_flows_backfill_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeFetcher({"https://nsearchives": PARTICIPANT_CSV.encode()})
    monkeypatch.setattr(cli, "Fetcher", lambda: fake)
    assert cli.main(["flows-backfill", "--start", "2026-09-28", "--end", "2026-09-30"]) == 0
    assert "fetched 3" in capsys.readouterr().out
    fake.pages = {"https://nsearchives": FetchError("HTTP 404")}
    assert cli.main(["flows-backfill", "--start", "2026-10-05", "--end", "2026-10-06"]) == 1


T0 = 1791171900


def _fakes(monkeypatch: pytest.MonkeyPatch, history_ok: bool = True) -> FakeFetcher:
    cfg = load_settings().broker.dhan
    fake = FakeDhan()

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/charts/intraday"):
            return httpx.Response(200, json={"timestamp": [T0, T0 + 60], "close": [1.0, 2.0]})
        if path.endswith("/charts/rollingoption"):
            body: dict[str, Any] = json.loads(req.content)
            side = {
                "timestamp": [T0],
                "close": [9.0],
                "oi": [5],
                "iv": [12.0],
                "spot": [1.0],
                "strike": [25000],
            }
            return httpx.Response(
                200,
                json={
                    "data": {"ce": side if history_ok else None, "pe": None},
                    "x": body["strike"],
                },
            )
        return fake.handler(req)

    def client(*_: object, **__: object) -> DhanClient:
        return DhanClient(
            "1", "tok-tests", cfg, transport=httpx.MockTransport(handler), sleep=lambda s: None
        )

    monkeypatch.setattr(selftest, "dhan_client", client)
    monkeypatch.setattr(
        selftest, "dhan_adapter", lambda *a, **k: (DhanAdapter(client(), cfg, FUT_ID), "NIFTY-FUT")
    )
    feed = rss([("Nifty rises", "https://et.test/1", None)])
    return FakeFetcher(
        {
            "https://nsearchives": PARTICIPANT_CSV.encode(),
            "https://fred.stlouisfed.org": fred_csv(("2026-10-01", "100"), ("2026-10-02", "101")),
            "https://economictimes": feed,
            "https://www.rbi.org.in": feed,
        }
    )


def test_selftest_all_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fetcher = _fakes(monkeypatch)
    res = selftest.run_selftest(
        load_settings(),
        Secrets(_env_file=None),
        load_news_config(),
        TradingCalendar.load(),
        tmp_path,
        fetcher,
        telegram=False,
    )
    assert [r.name for r in res if not r.ok] == [], res
    assert "COMPARE" in res[1].detail and "ATM call 1 rows" in res[2].detail


def test_selftest_reports_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fetcher = _fakes(monkeypatch, history_ok=False)
    fetcher.pages = {}
    res = selftest.run_selftest(
        load_settings(),
        Secrets(_env_file=None),
        load_news_config(),
        TradingCalendar.load(),
        tmp_path,
        fetcher,
        telegram=True,
    )
    failed = {r.name for r in res if not r.ok}
    assert failed == {
        "telegram",
        "dhan history",
        "NSE participant OI",
        "FRED global cues",
        "news feeds",
    }
    monkeypatch.setattr(cli, "Fetcher", lambda: fetcher)
    assert cli.main(["selftest", "--no-telegram"]) == 1
    assert "FAILED:" in capsys.readouterr().out


def test_selftest_hints() -> None:
    def boom() -> str:
        raise RuntimeError("/charts/intraday: HTTP 401 DH-902 not subscribed")

    r = selftest._run("dhan history", boom)
    assert not r.ok and "Data API subscription" in r.detail
