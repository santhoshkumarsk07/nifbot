"""fetch -> parse -> dedupe -> relevance -> sentiment -> impact -> store."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from defusedxml.common import DefusedXmlException
from pydantic import ValidationError

from nifbot.net import Fetcher, FetchError
from nifbot.news.dedupe import Deduper
from nifbot.news.impact import classify
from nifbot.news.models import NewsItem, ScoredNews
from nifbot.news.relevance import RelevanceFilter
from nifbot.news.rss import parse_feed
from nifbot.news.sentiment import SentimentScorer
from nifbot.news.sources import NewsConfig, Source
from nifbot.news.store import NewsStore
from nifbot.timeutil import now_ist

log = logging.getLogger(__name__)
MAX_AGE = timedelta(days=2)  # ignore stale feed entries


@dataclass
class RunResult:
    new: list[ScoredNews] = field(default_factory=list)
    fetched: int = 0
    errors: dict[str, str] = field(default_factory=dict)


class NewsPipeline:
    def __init__(
        self,
        cfg: NewsConfig,
        fetcher: Fetcher,
        store: NewsStore,
        scorer: SentimentScorer,
        clock: Callable[[], datetime] = now_ist,
    ) -> None:
        self._cfg = cfg
        self._fetcher = fetcher
        self._store = store
        self._scorer = scorer
        self._clock = clock
        self._relevance = RelevanceFilter(cfg.topics)
        self._last_poll: dict[str, datetime] = {}
        recent = store.recent_titles(clock() - MAX_AGE)
        self._dedupe = Deduper([u for u, _ in recent], [t for _, t in recent])

    def due(self, now: datetime) -> list[Source]:
        out = []
        for s in self._cfg.sources:
            last = self._last_poll.get(s.id)
            if s.enabled and (last is None or now - last >= timedelta(minutes=s.poll_minutes)):
                out.append(s)
        return out

    def _fetch_source(self, src: Source, now: datetime) -> list[NewsItem]:
        raw = parse_feed(self._fetcher.get(src.url))
        items: list[NewsItem] = []
        for e in raw:
            pub = e["published_at"]
            if isinstance(pub, datetime) and now - pub > MAX_AGE:
                continue
            try:
                items.append(
                    NewsItem(
                        source_id=src.id,
                        source_name=src.name,
                        official=src.official,
                        title=str(e["title"]),
                        url=str(e["url"]),
                        summary=str(e["summary"]),
                        published_at=pub if isinstance(pub, datetime) else None,
                        fetched_at=self._clock(),
                    )
                )
            except ValidationError:
                continue
        return items

    def run_once(self, force: bool = False) -> RunResult:
        """Poll every due source once. A failing source never stops the others."""
        now = self._clock()
        result = RunResult()
        candidates: list[tuple[NewsItem, tuple[str, ...]]] = []
        sources = [s for s in self._cfg.sources if s.enabled] if force else self.due(now)
        for src in sources:
            self._last_poll[src.id] = now
            try:
                items = self._fetch_source(src, now)
            except (FetchError, DefusedXmlException, SyntaxError) as exc:
                msg = f"{type(exc).__name__}: {exc}"[:300]
                result.errors[src.id] = msg
                self._store.record_health(src.id, now, msg)
                log.warning("news source %s failed: %s", src.id, msg)
                continue
            self._store.record_health(src.id, now, None)
            result.fetched += len(items)
            for it in items:
                if self._dedupe.seen(it.url, it.title):
                    continue
                tags = self._relevance.tags(f"{it.title} {it.summary}")
                if src.always_relevant and not tags:
                    tags = ("official",)
                if tags:
                    candidates.append((it, tags))
        if candidates:
            scores = self._scorer.score([f"{it.title}. {it.summary}"[:512] for it, _ in candidates])
            for (it, tags), sent in zip(candidates, scores, strict=True):
                scored = ScoredNews(
                    item=it,
                    tags=tags,
                    sentiment=round(sent, 3),
                    impact=classify(tags, sent, it.official),
                )
                if self._store.add(scored):
                    result.new.append(scored)
        return result
