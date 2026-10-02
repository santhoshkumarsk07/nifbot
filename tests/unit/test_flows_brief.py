"""Participant OI, FII/DII, FRED cues, flow store, brief and alerts."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from nifbot import DISCLAIMER
from nifbot.data.models import Quote
from nifbot.flows import fii_dii, participant_oi
from nifbot.flows.global_cues import Cue, fetch_cues, latest_cue, parse_fred_csv
from nifbot.flows.store import FlowStore
from nifbot.news.models import NewsItem, ScoredNews
from nifbot.news.store import connect
from nifbot.notify.messages import BriefInputs, news_alert, premarket_brief, premarket_tilt
from nifbot.timeutil import IST
from nifbot.trading_calendar import MarketEvent
from tests.fakes_web import FII_DII_JSON, PARTICIPANT_CSV, FakeFetcher, fred_csv

DAY = date(2026, 10, 1)
NOW = datetime(2026, 10, 5, 8, 45, tzinfo=IST)


def test_participant_oi_parse() -> None:
    rows = participant_oi.parse(PARTICIPANT_CSV, DAY)
    assert set(rows) == {"Client", "DII", "FII", "Pro"}
    fii = rows["FII"]
    assert fii.fut_idx_long == 100000 and fii.fut_long_ratio == 0.25 and fii.fut_net == -200000
    assert rows["Client"].fut_idx_long == 300000  # quoted thousands separator
    url = participant_oi.URL.format(d=DAY)
    assert url.endswith("fao_participant_oi_01102026.csv")
    got = participant_oi.fetch(FakeFetcher({url: PARTICIPANT_CSV.encode()}), DAY)
    assert got["FII"].day == DAY


@pytest.mark.parametrize(
    "text",
    [
        "nothing here",
        "Client Type,Future Index Long\nFII,1\n",
        PARTICIPANT_CSV.replace("FII,100000", "FII,abc"),
        PARTICIPANT_CSV.replace("FII,100000", "FII,-5"),
        PARTICIPANT_CSV.replace("\nFII,", "\nXYZ,"),
    ],
)
def test_participant_oi_rejects(text: str) -> None:
    with pytest.raises(participant_oi.ParticipantOIError):
        participant_oi.parse(text, DAY)


def test_fii_dii() -> None:
    flow = fii_dii.parse(FII_DII_JSON)
    assert flow == fii_dii.CashFlow(DAY, -2500.5, 3000.1)
    for bad in (b"nope", b"{}", b'[{"category":"DII","date":"x","netValue":"1"}]'):
        with pytest.raises(fii_dii.FiiDiiError):
            fii_dii.parse(bad)
    flows = [fii_dii.CashFlow(DAY + timedelta(days=i), -100.0 * i, 50.0) for i in range(5)]
    assert fii_dii.trend(flows, 5) == (-1000.0, 250.0)
    assert fii_dii.trend(flows, 20) is None


def test_fred() -> None:
    text = "observation_date,SP500\n2026-09-29,6000\n2026-09-30,.\n2026-10-01,6060\n"
    assert parse_fred_csv(text) == [(date(2026, 9, 29), 6000.0), (date(2026, 10, 1), 6060.0)]
    cue = latest_cue("S&P 500", "SP500", "pct", text)
    assert cue is not None and cue.change == pytest.approx(1.0) and "+1.00%" in cue.render()
    y = Cue("US 10Y yield", "DGS10", DAY, 4.25, 4.20, "level")
    assert y.change == pytest.approx(5) and "+5 bp" in y.render()
    assert latest_cue("x", "X", "pct", "DATE,X\n2026-10-01,1\n") is None
    assert parse_fred_csv("") == []
    fetcher = FakeFetcher(
        {
            "https://fred.stlouisfed.org/graph/fredgraph.csv?id=SP500": fred_csv(
                ("2026-09-30", "6000"), ("2026-10-01", "5940")
            )
        }
    )
    cues, missing = fetch_cues(fetcher)
    assert [c.series for c in cues] == ["SP500"] and "Dow Jones" in missing


def test_flow_store(tmp_path: Path) -> None:
    store = FlowStore(connect(tmp_path / "f.sqlite3"))
    store.add_cash(fii_dii.CashFlow(DAY, -100, 200), "manual")
    store.add_cash(fii_dii.CashFlow(DAY + timedelta(days=4), 1, 1), "manual")
    assert store.cash_until(DAY + timedelta(days=4)) == [fii_dii.CashFlow(DAY, -100, 200)]
    store.add_participant(participant_oi.parse(PARTICIPANT_CSV, DAY))
    hist = store.participant_history("FII", DAY + timedelta(days=1))
    assert len(hist) == 1 and hist[0].fut_idx_short == 300000
    assert store.participant_history("FII", DAY) == []  # strictly before


def _news(sent: float, impact: str) -> ScoredNews:
    item = NewsItem(
        source_id="s",
        source_name="ET",
        title="Nifty set for a gap-up open",
        url="https://x.test/1",
        fetched_at=NOW - timedelta(hours=1),
    )
    return ScoredNews(item=item, tags=("index",), sentiment=sent, impact=impact)


def test_brief_with_missing_inputs_says_not_available() -> None:
    text = premarket_brief(
        BriefInputs(day=NOW.date(), missing_cues=["S&P 500"], holiday_list_verified=False)
    )
    for phrase in (
        "GIFT Nifty: not available",
        "FII/DII cash: not available",
        "FII participant OI: not available",
        "India VIX: not available",
        "not available: S&P 500",
        "neutral (+0.00)",
        "not yet verified",
    ):
        assert phrase in text
    assert text.endswith(DISCLAIMER)


def test_brief_full_and_tilt() -> None:
    cues = [
        Cue("S&P 500", "SP500", DAY, 6060, 6000, "pct"),
        Cue("Brent crude", "DCOILBRENTEU", DAY, 70, 72, "pct"),
        Cue("Nikkei 225", "NIKKEI225", DAY, 100, 101, "pct"),
    ]
    flows = [fii_dii.CashFlow(DAY - timedelta(days=i), 1500.0, -200.0) for i in range(20, -1, -1)]
    oi = participant_oi.parse(PARTICIPANT_CSV, DAY)["FII"]
    vix = Quote(symbol="INDIAVIX", security_id="21", ltp=12.5, received_at=NOW)
    inp = BriefInputs(
        day=NOW.date(),
        cues=cues,
        cash_flows=flows,
        fii_oi=[oi, oi],
        vix=vix,
        news=[_news(0.6, "high")],
        news_score=0.4,
        events=[MarketEvent(date=NOW.date(), time="10:00", name="RBI policy", impact="high")],
    )
    text = premarket_brief(inp)
    assert "FII/DII cash 01 Oct: FII +1,500 cr" in text and "20-day" in text
    assert "long 25%" in text and "change +0" in text and "India VIX: 12.50" in text
    assert "10:00 RBI policy [high]" in text and "[+] Nifty set for a gap-up open" in text
    score, reasons = premarket_tilt(inp)
    assert -1 <= score <= 1 and any(r.startswith("US indices +") for r in reasons)
    assert "FII index futures -" in reasons


def test_news_alert_format() -> None:
    msg = news_alert(_news(-0.5, "high"))
    assert msg.startswith("[-] NEWS (high)") and "https://x.test/1" in msg and "ET" in msg
