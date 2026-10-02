"""News records. All fetched text is untrusted data, never instructions."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Impact = Literal["high", "medium", "low"]


class NewsItem(BaseModel):
    """A headline as fetched from a source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    source_name: str
    official: bool = False
    title: str = Field(min_length=3, max_length=500)
    url: str
    summary: str = Field(default="", max_length=2000)
    published_at: datetime | None = None
    fetched_at: datetime


class ScoredNews(BaseModel):
    """A relevant headline after scoring."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    item: NewsItem
    tags: tuple[str, ...]
    sentiment: float = Field(ge=-1, le=1)
    impact: Impact

    @property
    def sign(self) -> str:
        """Telegram tag: +, - or 0."""
        if self.sentiment >= 0.15:
            return "+"
        if self.sentiment <= -0.15:
            return "-"
        return "0"
