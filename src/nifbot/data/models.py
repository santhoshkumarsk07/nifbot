"""Normalised market-data records shared by live, recorder, replay and backtest.

``received_at`` is always our own IST clock at the moment the response arrived;
features may only use data whose ``received_at`` is at or before their timestamp.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

OptionType = Literal["CE", "PE"]


class _Rec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("received_at", check_fields=False)
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("received_at must be timezone-aware")
        return value


class Quote(_Rec):
    """Latest quote for a single instrument (index, future, ...)."""

    symbol: str
    security_id: str
    ltp: float = Field(gt=0)
    open: float | None = None
    high: float | None = None
    low: float | None = None
    prev_close: float | None = None
    volume: int | None = Field(default=None, ge=0)
    oi: int | None = Field(default=None, ge=0)
    avg_price: float | None = None
    bid: float | None = None
    ask: float | None = None
    received_at: datetime


class OptionQuote(_Rec):
    """One strike/side of the option chain."""

    strike: float = Field(gt=0)
    option_type: OptionType
    ltp: float = Field(ge=0)
    bid: float | None = Field(default=None, ge=0)
    ask: float | None = Field(default=None, ge=0)
    bid_qty: int | None = Field(default=None, ge=0)
    ask_qty: int | None = Field(default=None, ge=0)
    oi: int = Field(ge=0)
    prev_oi: int | None = Field(default=None, ge=0)
    volume: int = Field(ge=0)
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    security_id: str | None = None

    @property
    def change_in_oi(self) -> int | None:
        return None if self.prev_oi is None else self.oi - self.prev_oi


class OptionChain(_Rec):
    """Full chain for one expiry."""

    underlying: str
    expiry: date
    underlying_ltp: float = Field(gt=0)
    rows: tuple[OptionQuote, ...]
    received_at: datetime


class Snapshot(_Rec):
    """Everything polled in one cycle. Missing parts are ``None`` (never invented)."""

    received_at: datetime
    spot: Quote | None = None
    futures: Quote | None = None
    vix: Quote | None = None
    chain: OptionChain | None = None
    errors: tuple[str, ...] = ()
