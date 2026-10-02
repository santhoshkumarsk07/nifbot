"""News source definitions (config/news.yaml)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nifbot.config import CONFIG_DIR, load_yaml


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9_]{2,40}$")
    name: str
    url: str
    kind: str = "rss"
    official: bool = False
    enabled: bool = True
    poll_minutes: int = Field(default=5, ge=2, le=1440)
    always_relevant: bool = False

    @field_validator("url")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("source URLs must be https://")
        return v


class NewsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sentiment_engine: str = "lexicon"
    half_life_minutes: float = Field(default=120, gt=0)
    sources: tuple[Source, ...]
    topics: dict[str, tuple[str, ...]]


def load_news_config(path: Path | None = None) -> NewsConfig:
    cfg = NewsConfig.model_validate(load_yaml(path or CONFIG_DIR / "news.yaml"))
    ids = [s.id for s in cfg.sources]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate source ids in news.yaml")
    return cfg
