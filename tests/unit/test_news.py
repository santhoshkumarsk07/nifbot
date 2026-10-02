"""RSS parsing, dedupe, relevance, sentiment, impact, store, score, pipeline."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from defusedxml.common import DefusedXmlException

from nifbot.net import FetchError
from nifbot.news.dedupe import Deduper, is_similar, normalize_url
from nifbot.news.impact import classify
from nifbot.news.models import NewsItem, ScoredNews
from nifbot.news.pipeline import NewsPipeline
from nifbot.news.relevance import RelevanceFilter
from nifbot.news.rss import parse_date, parse_feed
from nifbot.news.score import news_score
from nifbot.news.sentiment import FinbertScorer, LexiconScorer, make_scorer
from nifbot.news.sources import NewsConfig, Source, load_news_config
from nifbot.news.store import NewsStore, connect
from nifbot.timeutil import IST
from tests.fakes_web import FakeFetcher, rss

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=IST)


def test_parse_rss_and_atom() -> None:
    data = rss(
        [
            (
                "Nifty <b>jumps</b> &amp; Sensex rallies",
                "https://x.test/a?utm_source=t",
                NOW - timedelta(hours=1),
            ),
            ("insecure link", "http://x.test/b", None),
        ],
        desc="<p>Body</p>",
    )
    items = parse_feed(data)
    assert len(items) == 1
    assert items[0]["title"] == "Nifty jumps & Sensex rallies" and items[0]["summary"] == "Body"
    assert items[0]["published_at"] == NOW - timedelta(hours=1)
    atom = (
        b"<feed xmlns='http://www.w3.org/2005/Atom'><entry><title>Fed holds rates</title>"
        b"<link rel='alternate' href='https://fed.test/1'/><updated>2026-10-05T02:00:00Z"
        b"</updated><summary>s</summary></entry></feed>"
    )
    a = parse_feed(atom)
    assert a[0]["url"] == "https://fed.test/1"
    assert a[0]["published_at"] == datetime(2026, 10, 5, 7, 30, tzinfo=IST)


def test_xml_bomb_rejected() -> None:
    bomb = (
        b"<?xml version='1.0'?><!DOCTYPE r [<!ENTITY a 'aaaa'><!ENTITY b '&a;&a;'>]>"
        b"<rss><channel><item><title>&b;</title></item></channel></rss>"
    )
    with pytest.raises(DefusedXmlException):
        parse_feed(bomb)


def test_parse_date_variants() -> None:
    assert parse_date(None) is None and parse_date("garbage") is None
    assert parse_date("2026-10-05 09:00:00") == NOW  # naive = IST


def test_dedupe() -> None:
    assert normalize_url("https://www.X.test/a/?utm_source=x&id=2#frag") == "https://x.test/a?id=2"
    assert is_similar("Nifty ends 1% higher; banks lead", "Nifty ends 1% higher, banks lead")
    assert not is_similar("Nifty falls", "RBI keeps repo rate unchanged")
    assert not is_similar("", "x")
    d = Deduper()
    assert not d.seen("https://a.test/1", "Sensex jumps 500 points on bank rally")
    assert d.seen("https://a.test/1?utm_medium=x", "different")
    assert d.seen("https://b.test/9", "Sensex jumps 500 points on bank rally!")


def test_relevance_tags() -> None:
    f = RelevanceFilter(load_news_config().topics)
    assert f.tags("RBI keeps repo rate unchanged") == ("rbi_policy",)
    assert "heavyweights" in f.tags("HDFC Bank Q2 profit beats estimates")
    assert f.tags("Bollywood box office weekend") == ()
    assert f.tags("Fedex results") == ()  # whole words only


def test_sentiment_scorers() -> None:
    lex = LexiconScorer()
    pos, neg, neu, negated = lex.score(
        [
            "Nifty surges to record high as FIIs return",
            "Markets crash on war fears",
            "RBI policy today",
            "Sensex does not fall",
        ]
    )
    assert pos > 0.3 and neg < -0.3 and neu == 0 and negated > 0

    def fake_pipe() -> object:
        def run(texts: list[str], truncation: bool) -> list[list[dict[str, object]]]:
            return [
                [
                    {"label": "positive", "score": 0.8},
                    {"label": "negative", "score": 0.1},
                    {"label": "neutral", "score": 0.1},
                ]
                for _ in texts
            ]

        return run

    assert FinbertScorer(fake_pipe).score(["x"]) == [pytest.approx(0.7)]
    assert make_scorer("lexicon").name == "lexicon"
    assert make_scorer("finbert").name in ("finbert", "lexicon")


def test_impact_rules() -> None:
    assert classify(("rbi_policy",), 0.0, official=True) == "high"
    assert classify(("index",), -0.6, official=False) == "high"
    assert classify(("index",), 0.0, official=False) == "medium"
    assert classify(("heavyweights",), 0.5, official=False) == "medium"
    assert classify(("heavyweights",), 0.0, official=False) == "low"
    assert classify(("official",), 0.0, official=True) == "low"


def _scored(title: str, sent: float, impact: str, at: datetime) -> ScoredNews:
    item = NewsItem(
        source_id="s", source_name="S", title=title, url=f"https://x.test/{title}", fetched_at=at
    )
    return ScoredNews(item=item, tags=("index",), sentiment=sent, impact=impact)


def test_store_and_score(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db" / "n.sqlite3")
    if os.name == "posix":
        assert os.stat(tmp_path / "db" / "n.sqlite3").st_mode & 0o077 == 0

    store = NewsStore(conn)
    a = _scored("aaa", 0.8, "high", NOW - timedelta(hours=2))
    b = _scored("bbb", -0.5, "medium", NOW)
    assert store.add(a) and store.add(b) and not store.add(a)
    got = store.between(NOW - timedelta(hours=3), NOW)
    assert [g.item.title for g in got] == ["aaa", "bbb"] and got[0].tags == ("index",)
    store.mark_alerted(a.item.url)
    store.record_health("s", NOW, None)
    store.record_health("s", NOW, "boom")
    store.record_health("s", NOW, "boom")
    assert store.health()[0]["consecutive_failures"] == 2
    # decay + no look-ahead: b is not known 1 hour earlier
    early = news_score(got, NOW - timedelta(hours=1))
    assert early > 0 and news_score([b], NOW - timedelta(minutes=1)) == 0.0
    assert news_score(got, NOW) < early
    assert news_score([a], NOW) < news_score([a], NOW - timedelta(hours=2))


def _cfg(sources: list[Source]) -> NewsConfig:
    base = load_news_config()
    return NewsConfig(sentiment_engine="lexicon", sources=tuple(sources), topics=base.topics)


def test_pipeline(tmp_path: Path) -> None:
    sources = [
        Source(id="one", name="One", url="https://one.test/rss"),
        Source(id="two", name="Two", url="https://two.test/rss"),
        Source(
            id="rbi", name="RBI", url="https://rbi.test/rss", official=True, always_relevant=True
        ),
        Source(id="bad", name="Bad", url="https://bad.test/rss"),
        Source(id="off", name="Off", url="https://off.test/rss", enabled=False),
    ]
    pages: dict[str, bytes | Exception] = {
        "https://one.test": rss(
            [
                ("Nifty surges to record high", "https://one.test/1", NOW),
                ("Cricket score update", "https://one.test/2", NOW),
                ("Old Nifty story", "https://one.test/3", NOW - timedelta(days=5)),
            ]
        ),
        "https://two.test": rss([("Nifty surges to a record high", "https://two.test/1", NOW)]),
        "https://rbi.test": rss([("Auction of Government Securities", "https://rbi.test/1", NOW)]),
        "https://bad.test": FetchError("HTTP 500"),
    }
    store = NewsStore(connect(tmp_path / "n.sqlite3"))
    pipe = NewsPipeline(
        _cfg(sources), FakeFetcher(pages), store, LexiconScorer(), clock=lambda: NOW
    )
    res = pipe.run_once()
    titles = sorted(s.item.title for s in res.new)
    assert titles == ["Auction of Government Securities", "Nifty surges to record high"]
    assert res.errors.keys() == {"bad"}
    assert all("off.test" not in u for u in pipe._fetcher.calls)  # type: ignore[attr-defined]
    assert pipe.due(NOW) == [] and pipe.due(NOW + timedelta(minutes=5))
    assert pipe.run_once().new == []  # nothing due
    # second pipeline on same DB remembers what was seen
    pipe2 = NewsPipeline(
        _cfg(sources), FakeFetcher(pages), store, LexiconScorer(), clock=lambda: NOW
    )
    assert pipe2.run_once(force=True).new == []


def test_bad_xml_is_source_error(tmp_path: Path) -> None:
    src = [Source(id="xx", name="X", url="https://x.test/rss")]
    store = NewsStore(connect(tmp_path / "n.sqlite3"))
    pipe = NewsPipeline(
        _cfg(src),
        FakeFetcher({"https://x.test": b"<rss><oops"}),
        store,
        LexiconScorer(),
        clock=lambda: NOW,
    )
    assert "xx" in pipe.run_once().errors


def test_news_config_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Source(id="x", name="X", url="http://insecure.test")
    p = tmp_path / "n.yaml"
    p.write_text(
        "sources:\n  - {id: aa, name: A, url: 'https://a.test'}\n"
        "  - {id: aa, name: B, url: 'https://b.test'}\ntopics: {}\n"
    )
    with pytest.raises(ValueError):
        load_news_config(p)
