"""Token-bucket rate limiter with an injectable clock."""

from __future__ import annotations

import time
from collections.abc import Callable


class TokenBucket:
    """Allows ``capacity`` events per ``period`` seconds, refilling continuously."""

    def __init__(
        self,
        capacity: int,
        period: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if capacity <= 0 or period <= 0:
            raise ValueError("capacity and period must be positive")
        self._capacity = float(capacity)
        self._rate = capacity / period
        self._tokens = float(capacity)
        self._clock = clock
        self._sleep = sleep
        self._last = clock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
        self._last = now

    def try_acquire(self) -> bool:
        """Take a token if available, without blocking."""
        self._refill()
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False

    def acquire(self) -> None:
        """Block until a token is available."""
        while not self.try_acquire():
            self._sleep((1 - self._tokens) / self._rate)
