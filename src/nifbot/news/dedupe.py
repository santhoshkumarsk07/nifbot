"""Duplicate detection: normalised URL plus fuzzy title match."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING = re.compile(r"^(utm_|fbclid|gclid|ref$|ref_|from$|cmpid|ocid)", re.IGNORECASE)
_NORM = re.compile(r"[^a-z0-9 ]+")


def normalize_url(url: str) -> str:
    """Lower-case host, drop fragments, tracking params and trailing slashes."""
    parts = urlsplit(url.strip())
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if not _TRACKING.match(k)))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", parts.netloc.lower().removeprefix("www."), path, query, ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()[:32]


def normalize_title(title: str) -> str:
    return " ".join(_NORM.sub(" ", title.lower()).split())


def is_similar(a: str, b: str, threshold: float = 0.88) -> bool:
    """Fuzzy title equality (same story from two outlets / reworded headline)."""
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= threshold


class Deduper:
    """Remembers seen URLs and titles."""

    def __init__(self, urls: Iterable[str] = (), titles: Iterable[str] = ()) -> None:
        self._urls = {url_hash(u) for u in urls}
        self._titles = list(titles)

    def seen(self, url: str, title: str) -> bool:
        """True if duplicate; otherwise remembers it and returns False."""
        h = url_hash(url)
        if h in self._urls or any(is_similar(title, t) for t in self._titles[-2000:]):
            return True
        self._urls.add(h)
        self._titles.append(title)
        return False
