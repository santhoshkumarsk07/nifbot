"""Token bucket."""

from __future__ import annotations

import pytest

from nifbot.notify.ratelimit import TokenBucket


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def test_bucket_limits_and_refills() -> None:
    clock = Clock()
    bucket = TokenBucket(2, 60.0, clock=clock, sleep=clock.sleep)
    assert bucket.try_acquire() and bucket.try_acquire()
    assert not bucket.try_acquire()
    bucket.acquire()  # blocks (fake sleep) ~30s
    assert clock.t == pytest.approx(30.0)


def test_bucket_rejects_bad_args() -> None:
    with pytest.raises(ValueError):
        TokenBucket(0, 1.0)
