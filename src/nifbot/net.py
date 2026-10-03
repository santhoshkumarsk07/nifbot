"""Polite HTTPS fetcher shared by news and flows connectors.

* HTTPS only, certificate verification on, timeouts on every request.
* robots.txt is checked per host; if it cannot be read (other than 404) the
  fetch is refused, so we fail closed.
* Minimum interval per host, retries with exponential backoff, response size cap.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "nifbot/0.1 (personal market-research bot; low volume)"
MAX_BYTES = 5_000_000


class FetchError(RuntimeError):
    """A fetch failed or was refused."""


class Fetcher:
    """Fetch URLs politely. Inject ``transport``/``sleep``/``clock`` in tests."""

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        max_retries: int = 2,
        min_host_interval: float = 2.0,
        respect_robots: bool = True,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            verify=True,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        )
        self._retries = max_retries
        self._min = min_host_interval
        self._robots_on = respect_robots
        self._sleep = sleep
        self._clock = clock
        self._last: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_why: dict[str, str] = {}

    def close(self) -> None:
        self._client.close()

    def _throttle(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            wait = self._min - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last[host] = self._clock()

    def _raw_get(self, url: str) -> httpx.Response:
        host = urlsplit(url).netloc
        delay = 1.0
        for attempt in range(self._retries + 1):
            self._throttle(host)
            try:
                resp = self._client.get(url)
            except httpx.HTTPError as exc:
                if attempt == self._retries:
                    raise FetchError(f"{host}: network error ({type(exc).__name__})") from None
                self._sleep(delay)
                delay *= 2
                continue
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt < self._retries:
                self._sleep(delay)
                delay *= 2
                continue
            return resp
        raise FetchError(f"{host}: retries exhausted")  # pragma: no cover

    def _load_robots(self, base: str) -> RobotFileParser | None:
        """Parsed robots.txt; allow-all on 404/410; ``None`` (deny) if unreadable."""
        parser = RobotFileParser()
        try:
            resp = self._raw_get(f"{base}/robots.txt")
        except FetchError as exc:
            self._robots_why[base] = f"robots.txt unreadable ({exc})"
            return None
        if resp.status_code == 200:
            parser.parse(resp.text.splitlines())
            return parser
        if resp.status_code in (404, 410):
            parser.parse([])  # empty robots.txt allows everything
            return parser
        self._robots_why[base] = f"robots.txt unreadable (HTTP {resp.status_code}); not fetching"
        return None

    def allowed(self, url: str) -> bool:
        """Whether robots.txt allows our user agent to fetch ``url``."""
        if not self._robots_on:
            return True
        parts = urlsplit(url)
        key = f"{parts.scheme}://{parts.netloc}"
        if key not in self._robots:
            self._robots[key] = self._load_robots(key)
        rp = self._robots[key]
        return rp is not None and rp.can_fetch(USER_AGENT, url)

    def get(self, url: str) -> bytes:
        """GET ``url`` and return the body. Raises :class:`FetchError`."""
        if not url.startswith("https://"):
            raise FetchError("only https:// URLs are allowed")
        if not self.allowed(url):
            parts = urlsplit(url)
            why = self._robots_why.get(
                f"{parts.scheme}://{parts.netloc}", "robots.txt disallows this URL"
            )
            raise FetchError(f"{parts.netloc}: {why}")
        resp = self._raw_get(url)
        if resp.status_code != 200:
            raise FetchError(f"{urlsplit(url).netloc}: HTTP {resp.status_code}")
        body = resp.content
        if len(body) > MAX_BYTES:
            raise FetchError(f"{urlsplit(url).netloc}: response too large")
        return body
