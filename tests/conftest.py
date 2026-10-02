"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from nifbot.notify.ratelimit import TokenBucket
from nifbot.notify.telegram import TelegramBot

TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmnopq"
ALLOWED = 111


class FakeTelegram:
    """Records Bot API calls and replies with scripted responses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: list[httpx.Response | Exception] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body: dict[str, Any] = httpx.Response(200, content=request.content).json()
        self.calls.append((method, body))
        if self.responses:
            nxt = self.responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.calls)}})


@pytest.fixture
def fake_tg() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def make_bot(fake_tg: FakeTelegram) -> Callable[..., TelegramBot]:
    def _make(**kwargs: Any) -> TelegramBot:
        sleeps: list[float] = []
        return TelegramBot(
            TOKEN,
            kwargs.pop("allowed", [ALLOWED]),
            transport=httpx.MockTransport(fake_tg.handler),
            sleep=sleeps.append,
            limiter=kwargs.pop("limiter", TokenBucket(1000, 60.0, sleep=sleeps.append)),
            **kwargs,
        )

    return _make
