"""Settings (YAML, non-secret) and secrets (environment / .env only)."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from nifbot.timeutil import parse_hhmm

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _check_hhmm(value: str) -> str:
    parse_hhmm(value)
    return value


class RiskSettings(_Strict):
    """Hard risk limits enforced by the risk manager."""

    max_loss_per_trade_pct: float = Field(gt=0, le=5)
    daily_loss_cap_pct: float = Field(gt=0, le=20)
    max_consecutive_losses: int = Field(ge=1)
    max_calls_per_day: int = Field(ge=1)
    max_open_calls: int = Field(ge=1)
    no_new_calls_before: str
    no_new_calls_after: str
    flatten_alert_at: str
    stale_data_seconds: int = Field(gt=0)

    @field_validator("no_new_calls_before", "no_new_calls_after", "flatten_alert_at")
    @classmethod
    def _hhmm(cls, value: str) -> str:
        return _check_hhmm(value)


class SessionSettings(_Strict):
    """Daily schedule (IST)."""

    monitor_start: str
    monitor_end: str
    premarket_brief_at: str
    eod_summary_at: str

    @field_validator("monitor_start", "monitor_end", "premarket_brief_at", "eod_summary_at")
    @classmethod
    def _hhmm(cls, value: str) -> str:
        return _check_hhmm(value)


class DhanSettings(_Strict):
    """Dhan HQ v2 market-data settings (no order endpoints are used)."""

    base_url: str
    nifty_security_id: str
    vix_security_id: str
    futures_security_id: str | None = None
    scrip_master_url: str
    timeout_seconds: float = Field(gt=0, le=60)
    max_retries: int = Field(ge=0, le=6)
    quote_min_interval_seconds: float = Field(ge=0)
    chain_min_interval_seconds: float = Field(ge=0)
    history_min_interval_seconds: float = Field(ge=0)

    @field_validator("base_url", "scrip_master_url")
    @classmethod
    def _https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("only https:// URLs are allowed")
        return value


class BrokerSettings(_Strict):
    name: str
    data_only: bool = True
    dhan: DhanSettings


class RecorderSettings(_Strict):
    interval_seconds: int = Field(ge=10)
    data_dir: str


class OrderPlacementSettings(_Strict):
    enabled: bool = False


class TelegramSettings(_Strict):
    max_messages_per_minute: int = Field(gt=0, le=30)
    request_timeout_seconds: float = Field(gt=0, le=60)


class NewsSettings(_Strict):
    llm_scorer_enabled: bool = False


class StorageSettings(_Strict):
    sqlite_path: str
    parquet_dir: str


class LoggingSettings(_Strict):
    dir: str
    level: str = "INFO"
    max_bytes: int = Field(gt=0)
    backup_count: int = Field(ge=1)


class Settings(_Strict):
    """Top-level non-secret configuration loaded from ``config/settings.yaml``."""

    capital_inr: float = Field(gt=0)
    risk: RiskSettings
    session: SessionSettings
    broker: BrokerSettings
    recorder: RecorderSettings
    order_placement: OrderPlacementSettings
    telegram: TelegramSettings
    news: NewsSettings
    storage: StorageSettings
    logging: LoggingSettings


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML mapping with the safe loader."""
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


def load_settings(path: Path | None = None) -> Settings:
    """Load and validate settings."""
    return Settings.model_validate(load_yaml(path or CONFIG_DIR / "settings.yaml"))


class Secrets(BaseSettings):
    """Secrets read only from the environment or a git-ignored ``.env`` file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: SecretStr | None = None
    telegram_allowed_chat_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    dhan_client_id: SecretStr | None = None
    dhan_access_token: SecretStr | None = None

    @field_validator("telegram_allowed_chat_ids", mode="before")
    @classmethod
    def _split_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(part) for part in value.split(",") if part.strip()]
        if isinstance(value, int):
            return [value]
        return value

    def secret_values(self) -> list[str]:
        """All configured secret strings, for log redaction."""
        values = [self.telegram_bot_token, self.dhan_client_id, self.dhan_access_token]
        return [v.get_secret_value() for v in values if v is not None and v.get_secret_value()]


def insecure_permissions(path: Path) -> bool:
    """True if ``path`` exists and is readable or writable by group/others."""
    if not path.exists() or os.name != "posix":
        return False
    mode = path.stat().st_mode
    return bool(mode & (stat.S_IRWXG | stat.S_IRWXO))
