"""Wiring used by the CLI and (later) the scheduler: news, flows and the brief."""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from nifbot.config import PROJECT_ROOT, Settings
from nifbot.data.models import Quote
from nifbot.data.recorder import load_snapshots
from nifbot.flows import fii_dii, participant_oi
from nifbot.flows.global_cues import fetch_cues
from nifbot.flows.store import FlowStore
from nifbot.net import Fetcher, FetchError
from nifbot.news.pipeline import NewsPipeline
from nifbot.news.score import news_score
from nifbot.news.sentiment import make_scorer
from nifbot.news.sources import NewsConfig
from nifbot.news.store import NewsStore, connect
from nifbot.notify.messages import BriefInputs
from nifbot.trading_calendar import TradingCalendar

log = logging.getLogger(__name__)


def open_db(settings: Settings) -> sqlite3.Connection:
    return connect(PROJECT_ROOT / settings.storage.sqlite_path)


def news_pipeline(cfg: NewsConfig, conn: sqlite3.Connection, fetcher: Fetcher) -> NewsPipeline:
    return NewsPipeline(cfg, fetcher, NewsStore(conn), make_scorer(cfg.sentiment_engine))


def previous_trading_day(cal: TradingCalendar, day: date) -> date:
    d = day - timedelta(days=1)
    for _ in range(15):
        if cal.is_trading_day(d):
            return d
        d -= timedelta(days=1)
    raise ValueError(f"no trading day found before {day}")


def fetch_flows(
    settings: Settings, conn: sqlite3.Connection, fetcher: Fetcher, day: date
) -> list[str]:
    """Fetch enabled flow sources for ``day`` into the DB. Returns status lines."""
    store = FlowStore(conn)
    out: list[str] = []
    if settings.flows.participant_oi_enabled:
        try:
            rows = participant_oi.fetch(fetcher, day)
            store.add_participant(rows)
            fii = rows["FII"]
            out.append(f"participant OI {day}: FII fut long {fii.fut_long_ratio:.0%}")
        except (FetchError, participant_oi.ParticipantOIError) as exc:
            out.append(f"participant OI {day}: FAILED ({exc})")
    if settings.flows.fii_dii_enabled:
        try:
            flow = fii_dii.parse(fetcher.get(fii_dii.URL))
            store.add_cash(flow, "nse")
            out.append(
                f"FII/DII {flow.day}: FII {flow.fii_net_cr:+,.0f} DII {flow.dii_net_cr:+,.0f}"
            )
        except (FetchError, fii_dii.FiiDiiError) as exc:
            out.append(f"FII/DII: FAILED ({exc})")
    return out


def latest_recorded_vix(data_dir: Path, before: datetime) -> Quote | None:
    """Most recent recorded India VIX quote received before ``before``."""
    if not data_dir.exists():
        return None
    for day_path in sorted(data_dir.iterdir(), reverse=True):
        for name in ("snapshots.jsonl", "snapshots.jsonl.gz"):
            f = day_path / name
            if not f.exists():
                continue
            for snap in reversed(load_snapshots(f)):
                if snap.vix is not None and snap.vix.received_at < before:
                    return snap.vix
    return None


def brief_inputs(
    settings: Settings,
    news_cfg: NewsConfig,
    conn: sqlite3.Connection,
    fetcher: Fetcher,
    cal: TradingCalendar,
    now: datetime,
) -> BriefInputs:
    """Collect everything for the pre-market brief, as known at ``now``."""
    day = now.date()
    cues, missing = fetch_cues(fetcher)
    flows = FlowStore(conn)
    news = NewsStore(conn).between(now - timedelta(hours=settings.news.brief_news_hours), now)
    relevant = [n for n in news if n.impact in ("high", "medium")]
    return BriefInputs(
        day=day,
        cues=cues,
        missing_cues=missing,
        cash_flows=flows.cash_until(day),
        fii_oi=flows.participant_history("FII", day),
        vix=latest_recorded_vix(PROJECT_ROOT / settings.recorder.data_dir, now),
        news=relevant,
        news_score=news_score(news, now, news_cfg.half_life_minutes),
        events=cal.events_on(day),
        holiday_list_verified=cal.is_verified(day),
    )
