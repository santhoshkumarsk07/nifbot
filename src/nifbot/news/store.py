"""SQLite storage for scored news (file mode 600)."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

from nifbot.news.dedupe import url_hash
from nifbot.news.models import NewsItem, ScoredNews

_SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    url_hash     TEXT PRIMARY KEY,
    source_id    TEXT NOT NULL,
    source_name  TEXT NOT NULL,
    official     INTEGER NOT NULL,
    title        TEXT NOT NULL,
    url          TEXT NOT NULL,
    summary      TEXT NOT NULL,
    published_at TEXT,
    fetched_at   TEXT NOT NULL,
    tags         TEXT NOT NULL,
    sentiment    REAL NOT NULL,
    impact       TEXT NOT NULL,
    alerted      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS news_fetched ON news(fetched_at);
CREATE TABLE IF NOT EXISTS source_health (
    source_id  TEXT PRIMARY KEY,
    last_ok    TEXT,
    last_error TEXT,
    last_error_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);
"""


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def connect(path: Path) -> sqlite3.Connection:
    """Open (and create with 600 permissions) the SQLite database."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


class NewsStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._c = conn

    def add(self, scored: ScoredNews) -> bool:
        """Insert; returns False if the URL was already stored."""
        it = scored.item
        cur = self._c.execute(
            "INSERT OR IGNORE INTO news VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (
                url_hash(it.url),
                it.source_id,
                it.source_name,
                int(it.official),
                it.title,
                it.url,
                it.summary,
                it.published_at.isoformat() if it.published_at else None,
                it.fetched_at.isoformat(),
                ",".join(scored.tags),
                scored.sentiment,
                scored.impact,
            ),
        )
        self._c.commit()
        return cur.rowcount == 1

    def recent_titles(self, since: datetime) -> list[tuple[str, str]]:
        rows = self._c.execute(
            "SELECT url, title FROM news WHERE fetched_at >= ?", (since.isoformat(),)
        )
        return [(r["url"], r["title"]) for r in rows]

    def between(self, start: datetime, end: datetime) -> list[ScoredNews]:
        """Items fetched in [start, end] (by *fetched_at*, i.e. when we knew them)."""
        rows = self._c.execute(
            "SELECT * FROM news WHERE fetched_at >= ? AND fetched_at <= ? ORDER BY fetched_at",
            (start.isoformat(), end.isoformat()),
        )
        out = []
        for r in rows:
            item = NewsItem(
                source_id=r["source_id"],
                source_name=r["source_name"],
                official=bool(r["official"]),
                title=r["title"],
                url=r["url"],
                summary=r["summary"],
                published_at=_dt(r["published_at"]),
                fetched_at=datetime.fromisoformat(r["fetched_at"]),
            )
            tags = tuple(t for t in r["tags"].split(",") if t)
            out.append(
                ScoredNews(item=item, tags=tags, sentiment=r["sentiment"], impact=r["impact"])
            )
        return out

    def mark_alerted(self, url: str) -> None:
        self._c.execute("UPDATE news SET alerted = 1 WHERE url_hash = ?", (url_hash(url),))
        self._c.commit()

    def record_health(self, source_id: str, at: datetime, error: str | None) -> None:
        if error is None:
            self._c.execute(
                "INSERT INTO source_health(source_id,last_ok,consecutive_failures) VALUES(?,?,0) "
                "ON CONFLICT(source_id) DO UPDATE SET last_ok=excluded.last_ok, "
                "consecutive_failures=0",
                (source_id, at.isoformat()),
            )
        else:
            self._c.execute(
                "INSERT INTO source_health(source_id,last_error,last_error_at,consecutive_failures)"
                " VALUES(?,?,?,1) ON CONFLICT(source_id) DO UPDATE SET "
                "last_error=excluded.last_error, last_error_at=excluded.last_error_at, "
                "consecutive_failures=consecutive_failures+1",
                (source_id, error[:300], at.isoformat()),
            )
        self._c.commit()

    def health(self) -> list[sqlite3.Row]:
        return list(self._c.execute("SELECT * FROM source_health ORDER BY source_id"))
