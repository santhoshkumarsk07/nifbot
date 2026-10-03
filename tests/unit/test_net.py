"""Polite fetcher: https only, robots.txt, retries, throttle, size cap."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from nifbot import net
from nifbot.net import Fetcher, FetchError


def _fetcher(
    handler: Callable[[httpx.Request], httpx.Response], respect_robots: bool = True
) -> tuple[Fetcher, list[float]]:
    sleeps: list[float] = []
    f = Fetcher(
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        clock=lambda: 0.0,
        respect_robots=respect_robots,
    )
    return f, sleeps


def test_robots_allow_and_disallow() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /private\n")
        return httpx.Response(200, content=b"ok")

    f, sleeps = _fetcher(handler)
    assert f.get("https://a.test/feed.xml") == b"ok"
    with pytest.raises(FetchError, match="robots"):
        f.get("https://a.test/private/x")
    assert sleeps  # same host throttled
    f.close()


def test_robots_404_allows_and_5xx_denies() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "missing.test" and req.url.path == "/robots.txt":
            return httpx.Response(404)
        if req.url.path == "/robots.txt":
            return httpx.Response(403)
        return httpx.Response(200, content=b"ok")

    f, _ = _fetcher(handler)
    assert f.get("https://missing.test/x") == b"ok"
    with pytest.raises(FetchError):
        f.get("https://forbidden.test/x")


def test_https_only_and_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/robots.txt":
            return httpx.Response(404)
        calls["n"] += 1
        if req.url.path == "/flaky" and calls["n"] == 1:
            return httpx.Response(503)
        if req.url.path == "/gone":
            return httpx.Response(410)
        if req.url.path == "/net":
            raise httpx.ConnectError("x")
        return httpx.Response(200, content=b"x" * 10)

    f, _ = _fetcher(handler, respect_robots=False)
    with pytest.raises(FetchError, match="https"):
        f.get("http://a.test/x")
    assert f.get("https://a.test/flaky") == b"x" * 10
    with pytest.raises(FetchError, match="410"):
        f.get("https://a.test/gone")
    with pytest.raises(FetchError, match="network"):
        f.get("https://a.test/net")
    monkeypatch.setattr(net, "MAX_BYTES", 5)
    with pytest.raises(FetchError, match="too large"):
        f.get("https://a.test/big")


def test_robots_error_says_why() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "deny.test" and req.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        if req.url.path == "/robots.txt":
            return httpx.Response(403)
        return httpx.Response(200, content=b"ok")

    f, _ = _fetcher(handler)
    with pytest.raises(FetchError, match=r"robots\.txt disallows"):
        f.get("https://deny.test/x")
    with pytest.raises(FetchError, match=r"unreadable \(HTTP 403\)"):
        f.get("https://blocked.test/x")
