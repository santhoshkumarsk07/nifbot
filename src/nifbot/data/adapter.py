"""Broker adapter interface. Live, replay and test adapters all implement it."""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from nifbot.data.models import OptionChain, Quote


class DataError(RuntimeError):
    """Data could not be fetched or failed validation. Never contains secrets."""


@runtime_checkable
class BrokerAdapter(Protocol):
    """Read-only market data. There is deliberately no order method here."""

    def get_spot(self) -> Quote:
        """Nifty 50 index quote."""
        ...

    def get_futures(self) -> Quote:
        """Current-month Nifty futures quote (price + OI)."""
        ...

    def get_vix(self) -> Quote:
        """India VIX quote."""
        ...

    def get_expiries(self) -> list[date]:
        """Available option expiries, ascending."""
        ...

    def get_option_chain(self, expiry: date) -> OptionChain:
        """Full option chain for ``expiry``."""
        ...
