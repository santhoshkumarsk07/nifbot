"""CLI news/flows/brief commands with a fake web (no network)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from nifbot import cli, services
from nifbot.config import load_settings
from nifbot.data.models import Quote, Snapshot
from nifbot.net import FetchError
from nifbot.timeutil import IST, now_ist
from nifbot.trading_calendar import TradingCalendar
from tests.fakes_web import FII_DII_JSON, PARTICIPANT_CSV, FakeFetcher, fred_csv, rss


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeFetcher:
    now = now_ist()
    pages: dict[str, bytes | Exception] = {
        "https://economictimes.indiatimes.com": rss(
            [("Sensex crashes 900 points as FIIs sell", "https://et.test/1", now)]
        ),
        "https://www.rbi.org.in": rss(
            [("RBI keeps repo rate unchanged", "https://rbi.test/1", now)]
        ),
        "https://nsearchives.nseindia.com/content/nsccl": PARTICIPANT_CSV.encode(),
        "https://www.nseindia.com/api": FII_DII_JSON,
        "https://fred.stlouisfed.org": fred_csv(("2026-09-30", "100"), ("2026-10-01", "101")),
        "https://www.moneycontrol.com": FetchError("HTTP 403"),
    }
    fake = FakeFetcher(pages)
    monkeypatch.setattr(cli, "Fetcher", lambda: fake)
    monkeypatch.setattr(services, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(cli, "ENV_FILE", tmp_path / ".env")
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_IDS"):
        monkeypatch.delenv(var, raising=False)
    return fake


def test_news_once_and_health(web: FakeFetcher, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["news-once"]) == 0
    out = capsys.readouterr().out
    assert "Sensex crashes 900 points" in out and "RBI keeps repo rate" in out
    assert "moneycontrol_markets: FAILED" in out
    assert cli.main(["news-health"]) == 0
    health = capsys.readouterr().out
    assert "OK      et_markets" in health and "ERR x1  moneycontrol_markets" in health
    assert cli.main(["news-once", "--alerts"]) == 1  # no Telegram token configured


def test_news_health_empty(web: FakeFetcher, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["news-health"]) == 0
    assert "no data yet" in capsys.readouterr().out


def test_flows_and_brief(web: FakeFetcher, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["flows-fetch", "2026-10-01"]) == 0
    assert "FII fut long 25%" in capsys.readouterr().out
    assert cli.main(["flows-add", "2026-10-01", "--", "-2500", "3000"]) == 0
    assert "FII -2,500 cr" in capsys.readouterr().out
    cli.main(["news-once"])
    capsys.readouterr()
    assert cli.main(["brief", "--force"]) == 0
    text = capsys.readouterr().out
    assert "PRE-MARKET BRIEF" in text and "S&P 500" in text and "Sensex crashes" in text
    assert cli.main(["brief", "--force", "--send"]) == 1  # no Telegram configured
    assert cli.main(["news-watch"]) == 1


def test_flows_fetch_failure(web: FakeFetcher, capsys: pytest.CaptureFixture[str]) -> None:
    web.pages["https://nsearchives.nseindia.com/content/nsccl"] = FetchError("HTTP 404")
    assert cli.main(["flows-fetch", "2026-10-01"]) == 1
    assert "FAILED" in capsys.readouterr().out


def test_fii_dii_when_enabled(web: FakeFetcher, tmp_path: Path) -> None:
    settings = load_settings()
    on = settings.model_copy(
        update={"flows": settings.flows.model_copy(update={"fii_dii_enabled": True})}
    )
    lines = services.fetch_flows(on, services.open_db(settings), web, date(2026, 10, 1))
    assert any(line.startswith("FII/DII 2026-10-01: FII -2,500") for line in lines)
    web.pages["https://www.nseindia.com/api"] = b"garbage"
    lines = services.fetch_flows(on, services.open_db(settings), web, date(2026, 10, 1))
    assert any("FII/DII: FAILED" in line for line in lines)


def test_previous_trading_day_and_vix(tmp_path: Path) -> None:
    cal = TradingCalendar.load()
    assert services.previous_trading_day(cal, date(2026, 10, 5)) == date(2026, 10, 1)
    assert services.latest_recorded_vix(tmp_path / "none", now_ist()) is None
    d = tmp_path / "2026-10-01"
    d.mkdir()
    t = datetime(2026, 10, 1, 15, 30, tzinfo=IST)
    snap = Snapshot(
        received_at=t, vix=Quote(symbol="INDIAVIX", security_id="21", ltp=11.8, received_at=t)
    )
    (d / "snapshots.jsonl").write_text(snap.model_dump_json() + "\n")
    (tmp_path / "2026-10-02").mkdir()
    vix = services.latest_recorded_vix(tmp_path, t + timedelta(days=4))
    assert vix is not None and vix.ltp == 11.8
    assert services.latest_recorded_vix(tmp_path, t) is None


def test_brief_skips_holiday(
    web: FakeFetcher, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "now_ist", lambda: datetime(2026, 10, 2, 8, 45, tzinfo=IST))
    assert cli.main(["brief"]) == 0
    assert "not a trading day" in capsys.readouterr().out
    monkeypatch.setattr(cli, "now_ist", lambda: datetime(2031, 1, 6, 8, 45, tzinfo=IST))
    assert cli.main(["brief"]) == 1


def test_flows_import(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    folder = tmp_path / "nse"
    folder.mkdir()
    (folder / "fao_participant_oi_01102026.csv").write_text(PARTICIPANT_CSV)
    (folder / "random.csv").write_text("x")
    (folder / "fao_participant_oi_02102026.csv").write_text("garbage")
    assert cli.main(["flows-import", str(folder)]) == 0
    out = capsys.readouterr().out
    assert "imported 1 day(s), skipped 2" in out
    assert cli.main(["flows-import", str(tmp_path / "empty")]) == 1
    from nifbot.flows.participant_oi import day_from_filename

    assert day_from_filename("fao_participant_oi_31022026.csv") is None
